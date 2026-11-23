# RF Presence Sense — Passive Positionsbestimmung von Menschen & Tieren über bestehende WLAN- & Bluetooth-Geräte

**Keine extra Hardware. Kein ESP32. Kein Bluetooth-Proxy.**

`RF Presence Sense` ist eine Custom-Integration für Home Assistant, die **ausschließlich bereits eingebundene WLAN- und Bluetooth-Geräte** (Smart Plugs, Lampen, Schalter, Router/APs, BLE-Sensoren, TVs, etc.) nutzt, um **gerätelos (device-free)** die 2D-Positionen von Menschen und Tieren zu erfassen.

---

## Wie funktioniert das physikalisch? (Radio Tomographic Imaging — RTI + VRTI)

1. **Wasser absorbiert 2,4-GHz- und 5-GHz-Funkwellen:**
   Menschen und Tiere bestehen zu ~60–70 % aus Wasser. Betritt ein Lebewesen die **1. Fresnel-Zone** zwischen einem stationären WLAN-Gerät und dem Router (oder einem stationären BLE-Gerät und dem Home-Assistant-Bluetooth-Adapter), verändert sich das Funkfeld messbar:
   - **Shadowing (Dämpfung $\Delta y$):** Der RSSI-Wert fällt um typischerweise 2–10 dB ab.
   - **VRTI (Variance-Based RTI):** Selbst bei starker Mehrwegeausbreitung (Multipath) steigt die kurzfristige **Varianz** des RSSI-Signals bei jeder Bewegung drastisch an.
2. **2D-Tomographie (Woodbury-Dual-Inversion):**
   Die Integration rekonstruiert aus allen aktiven WLAN- und BLE-Links in Echtzeit (< 1 ms in reinem Python, ohne schwere Abhängigkeiten) eine **2D-Dämpfungs-Heatmap** über deinen Grundriss.
3. **Multi-Target Kalman-Tracker:**
   Erkennt lokale Maxima mit Sub-Voxel-Genauigkeit, verfolgt mehrere Ziele gleichzeitig (Position $(x, y)$ + Geschwindigkeit) und ordnet sie deinen Räumen zu.
4. **3D-Höhen- & Querschnitts-Klassifikation (Mensch vs. Tier):**
   Jedes stationäre Gerät besitzt eine Montagehöhe $z$ (z. B. Smart-Plug an der Steckdose $z \approx 0{,}35\text{ m}$ vs. Lampe/Sensor $z \approx 1{,}8\text{ m}$):
   - **Tier (Katze/Hund):** Stört primär bodennahe Links ($z < 0{,}9\text{ m}$) bei kleinerem Funkquerschnitt, während höherliegende Links darüber ruhig bleiben.
   - **Mensch:** Stört sowohl niedrige als auch höhere Links ($z \ge 0{,}9\text{ m}$) mit stärkerer Dämpfung und größerem räumlichen Schatten.

---

## Unterstützte bestehende Datenquellen in Home Assistant

- **WLAN- & IoT-RSSI-Entitäten:** Automatische Erkennung aller Sensoren mit `device_class: signal_strength`, Einheit `dBm` oder Namen wie `*_rssi`, `*_wifi_signal`, `*_signal_strength` (z. B. Shelly, Tasmota, ESPHome, TP-Link/Kasa, Meross, Fritz!Box, UniFi, OpenWrt, ASUSWRT).
- **`device_tracker`-Attribute:** Liest `rssi` / `signal_strength` direkt aus Router-Integrationen (UniFi, Fritz!Box, OpenWrt, MikroTik, Omada).
- **Zigbee/Thread-Bonus-Links:** Optional werden auch `linkquality` (LQI) Werte linear in Pseudo-dBm umgerechnet.
- **Natives Home Assistant Bluetooth:** Klinkt sich passiv (`BluetoothScanningMode.PASSIVE`) in den bereits vorhandenen Home-Assistant-Bluetooth-Stack ein und liest alle Werbepakete (Advertisements) stationärer BLE-Geräte in Echtzeit aus.

---

## Installation

1. Kopiere den Ordner `custom_components/rf_presence_sense` in dein Home-Assistant-Verzeichnis `/config/custom_components/rf_presence_sense`.
2. Starte Home Assistant neu.
3. Gehe zu **Einstellungen → Geräte & Dienste → Integration hinzufügen → „RF Presence Sense“**.
4. Gib deine Grundrissmaße und die 3D-Position von Router & Home-Assistant-Host ein und wähle deine **stationären** WLAN- und Bluetooth-Geräte aus.

---

## Einrichtung & Kalibrierung für maximale Genauigkeit

1. **Gerätepositionen $(x, y, z)$ hinterlegen:**
   Nutze den Service `rf_presence_sense.set_device_position` für deine wichtigsten Geräte (insbesondere die Höhe $z$ in Metern für die Mensch/Tier-Unterscheidung!).
2. **Räume definieren:**
   Nutze `rf_presence_sense.define_room`, um Raumzonen anzulegen.
3. **Leeren Raum kalibrieren:**
   Rufe `rf_presence_sense.calibrate_empty_room` (z. B. 20–30 Sekunden) auf, wenn niemand im Raum ist. Dadurch werden Grundrauschen und exakte Baseline jedes Links eingelernt.
4. **Live-Heatmap im Dashboard anzeigen:**
   Füge die Kamera-Entität `camera.rf_presence_sense_live_rf_heatmap` als *Picture Entity* Karte zu deinem Dashboard hinzu.

---

## Bereitgestellte Entitäten

| Entität | Beschreibung |
|---|---|
| `sensor.rf_presence_sense_erkannte_menschen` | Anzahl aktuell erkannter Menschen |
| `sensor.rf_presence_sense_erkannte_tiere` | Anzahl aktuell erkannter Tiere |
| `sensor.rf_presence_sense_erkannte_lebewesen_gesamt` | Gesamtzahl erkannter Lebewesen |
| `sensor.rf_presence_sense_positionen_ziele` | Status + Attribute mit exakten `(x, y)`-Koordinaten, Geschwindigkeit, Typ, Konfidenz & Raum |
| `sensor.rf_presence_sense_rf_netz_abdeckung` | Anteil aktiver RF-Links (%) & Paket-Zähler |
| `binary_sensor.rf_presence_sense_mensch_erkannt` | Global: Mensch erkannt |
| `binary_sensor.rf_presence_sense_tier_erkannt` | Global: Tier erkannt |
| `binary_sensor.rf_presence_sense_<raum>_prasenz` / `_mensch` / `_tier` | Raumbezogene Präsenz-Sensoren |
| `camera.rf_presence_sense_live_rf_heatmap` | Live-SVG-Karte mit Heatmap, RF-Links, Räumen & Zielmarkern |

---

## Praktische Tipps & physikalische Grenzen

- **Je mehr stationäre Geräte & sich kreuzende Links, desto genauer die Ortung:** Ideal sind 8–20+ stationäre Geräte (Steckdosen + Lampen + BLE-Sensoren), idealerweise über 2+ Empfänger (z. B. WLAN-Router + HA-Bluetooth-Adapter an unterschiedlichen Stellen, oder mehrere Mesh-APs).
- **Keine mobilen Geräte auswählen:** Handys oder Smartwatches verfälschen die Tomographie, weil sie sich selbst bewegen.
- **Abtastrate:** Natives Bluetooth liefert oft 1–5 Pakete/Sekunde (ideal!). Bei WLAN-Geräten (Shelly/Tasmota/UniFi) hängt die Reaktionszeit vom jeweiligen RSSI-Update-Intervall ab.
- **Realistische Genauigkeit:** Mit reinem RSSI (ohne Wi-Fi-CSI-Spezialhardware) sind je nach Link-Dichte **Raum- bis ~0,8–1,5 m Zonen-Genauigkeit** realistisch erreichbar.
