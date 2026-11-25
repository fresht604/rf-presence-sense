/**
 * RF Presence Sense — Interactive 3D Sidebar Panel & Custom Lovelace Card (v2.1.0)
 * Pure Vanilla JS + HTML5 3D Canvas (zero external dependencies, 0% idle CPU).
 *
 * Exports two Web Components:
 *   - <rf-presence-3d-card>  : Embeddable Lovelace 3D Card
 *   - <rf-presence-3d-panel> : Full-viewport Home Assistant Sidebar Panel ("3D RF-Map")
 */

const _EDGE_FULL = { n: "north", s: "south", e: "east", w: "west" };

function _normEdge(e) {
  const s = String(e || "").trim().toLowerCase();
  return _EDGE_FULL[s] || s;
}

class RFPresence3DCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._hass = null;

    // 3D CAD Camera state (radians, zoom, screen pan)
    this._yaw = (38 * Math.PI) / 180;
    this._pitch = (34 * Math.PI) / 180;
    this._zoom = 1.0;
    this._panX = 0;
    this._panY = 0;

    // Smooth orbit inertia velocities (auto-stop -> 0% idle CPU)
    this._velYaw = 0;
    this._velPitch = 0;

    // Interaction state
    this._mode = "view"; // "view" | "edit" | "calibrate"
    this._dragKind = null; // "orbit" | "pan" | "item"
    this._draggingItem = null;
    this._selectedItem = null;
    this._lastPtr = { x: 0, y: 0 };
    this._activeTouches = new Map();
    this._pinchDist = 0;
    this._pinchCenter = null;

    // Calibration wizard selection state
    this._calPoint = null; // { x, y, room_id }
    this._walkPreview = null; // { sx, sy, ex, ey }

    // Draft edits before Save
    this._draftPositions = {};
    this._draftRooms = {};

    // Floorplan image & render scheduling
    this._floorImg = null;
    this._floorImgUrl = null;
    this._rafId = null;
    this._lastPulseDraw = 0;
    this._lastStateObj = null;
    this._resolvedEntityId = null;
    this._lastRoomSig = "";
    this._isFullPanel = false;
  }

  static getStubConfig() {
    return {
      title: "RF Presence Sense 3D",
      entity: "sensor.rf_presence_sense_targets_detail",
      height: 460,
    };
  }

  setConfig(config) {
    if (!config) {
      throw new Error("Invalid configuration for rf-presence-3d-card");
    }
    this._isFullPanel = Boolean(config.full_panel);
    const safeHeight = Math.max(
      240,
      Math.min(1400, Math.round(Number(config.height) || 460))
    );
    this._config = {
      title: String(config.title || "RF Presence Sense 3D"),
      entity: String(config.entity || ""),
      floorplan_image: String(config.floorplan_image || ""),
      height: safeHeight,
      full_panel: this._isFullPanel,
    };
    this._resolvedEntityId = null;
    this._lastStateObj = null;
    this._loadFloorplanImage(this._config.floorplan_image);
    this._ensureDom();
    this._applyViewportSizing();
    this._updateHud();
    this._populateRoomDropdowns();
    this._renderScene();
  }

  set hass(hass) {
    this._hass = hass;
    this._ensureDom();
    const st = this._getStateObj();
    if (st === this._lastStateObj) {
      return;
    }
    this._lastStateObj = st;
    this._updateHud();
    this._populateRoomDropdowns();
    this._renderScene();
    this._syncAnimLoop();
  }

  getCardSize() {
    return Math.max(6, Math.round((this._config.height || 460) / 70));
  }

  connectedCallback() {
    this._ensureDom();
    this._applyViewportSizing();
    if (!this._windowBound) {
      this._boundPointerMove = (ev) => this._onPointerMove(ev);
      this._boundPointerUp = (ev) => this._onPointerUp(ev);
      this._boundVisibility = () => this._syncAnimLoop();
      this._boundResize = () => this._renderScene();
      window.addEventListener("pointermove", this._boundPointerMove);
      window.addEventListener("pointerup", this._boundPointerUp);
      window.addEventListener("pointercancel", this._boundPointerUp);
      window.addEventListener("resize", this._boundResize);
      document.addEventListener("visibilitychange", this._boundVisibility);
      this._windowBound = true;
    }
    this._renderScene();
    this._syncAnimLoop();
  }

  disconnectedCallback() {
    this._stopAnimLoop();
    this._velYaw = 0;
    this._velPitch = 0;
    this._dragKind = null;
    this._draggingItem = null;
    this._activeTouches.clear();
    if (this._windowBound) {
      window.removeEventListener("pointermove", this._boundPointerMove);
      window.removeEventListener("pointerup", this._boundPointerUp);
      window.removeEventListener("pointercancel", this._boundPointerUp);
      window.removeEventListener("resize", this._boundResize);
      document.removeEventListener("visibilitychange", this._boundVisibility);
      this._windowBound = false;
    }
    if (this._toastTimer) {
      clearTimeout(this._toastTimer);
      this._toastTimer = null;
    }
  }

  _applyViewportSizing() {
    if (!this.shadowRoot) return;
    const card = this.shadowRoot.querySelector("ha-card");
    const vp = this.shadowRoot.querySelector(".viewport");
    if (!card || !vp) return;
    if (this._isFullPanel) {
      card.classList.add("full-panel");
      vp.style.height = "";
    } else {
      card.classList.remove("full-panel");
      vp.style.height = `${this._config.height || 460}px`;
    }
  }

  _hasActiveAnimation() {
    const hasInertia =
      Math.abs(this._velYaw) > 0.0004 || Math.abs(this._velPitch) > 0.0004;
    if (hasInertia) return true;
    const st = this._getStateObj();
    if (!st || !st.attributes) return false;
    const attrs = st.attributes;
    return Boolean(
      (Array.isArray(attrs.targets) && attrs.targets.length > 0) ||
        attrs.calibrating ||
        attrs.edge_walk_active
    );
  }

  _stopAnimLoop() {
    if (this._rafId) {
      cancelAnimationFrame(this._rafId);
      this._rafId = null;
    }
  }

  _syncAnimLoop() {
    if (!this.isConnected || document.hidden || !this._hasActiveAnimation()) {
      this._stopAnimLoop();
      return;
    }
    if (this._rafId) return;
    const tick = (ts) => {
      if (!this.isConnected || document.hidden || !this._hasActiveAnimation()) {
        this._rafId = null;
        return;
      }
      let shouldDraw = false;
      if (Math.abs(this._velYaw) > 0.0004 || Math.abs(this._velPitch) > 0.0004) {
        this._yaw += this._velYaw;
        this._pitch = Math.max(
          (8 * Math.PI) / 180,
          Math.min((88 * Math.PI) / 180, this._pitch + this._velPitch)
        );
        this._velYaw *= 0.9;
        this._velPitch *= 0.9;
        if (Math.abs(this._velYaw) <= 0.0004) this._velYaw = 0;
        if (Math.abs(this._velPitch) <= 0.0004) this._velPitch = 0;
        shouldDraw = true;
      } else if (ts - this._lastPulseDraw >= 95) {
        shouldDraw = true;
        this._lastPulseDraw = ts;
      }
      if (shouldDraw) {
        this._renderScene();
      }
      this._rafId = requestAnimationFrame(tick);
    };
    this._rafId = requestAnimationFrame(tick);
  }

  _loadFloorplanImage(url) {
    if (!url) {
      this._floorImg = null;
      this._floorImgUrl = null;
      return;
    }
    if (url === this._floorImgUrl) return;
    this._floorImgUrl = url;
    const img = new Image();
    img.crossOrigin = "anonymous";
    img.onload = () => {
      this._floorImg = img;
      this._renderScene();
    };
    img.onerror = () => {
      this._floorImg = null;
    };
    img.src = url;
  }

  _getStateObj() {
    if (!this._hass || !this._hass.states) return null;
    if (this._config.entity && this._hass.states[this._config.entity]) {
      return this._hass.states[this._config.entity];
    }
    if (this._resolvedEntityId && this._hass.states[this._resolvedEntityId]) {
      return this._hass.states[this._resolvedEntityId];
    }
    for (const [eid, st] of Object.entries(this._hass.states)) {
      if (
        eid.startsWith("sensor.") &&
        st &&
        st.attributes &&
        Array.isArray(st.attributes.active_links_3d) &&
        st.attributes.geometry
      ) {
        this._resolvedEntityId = eid;
        return st;
      }
    }
    return null;
  }

  _decodeRooms(rawRooms) {
    return (rawRooms || []).map((rm) => {
      const draft = this._draftRooms[rm.id];
      const base = draft ? { ...rm, ...draft } : { ...rm };
      const rawOe = base.open_edges || base.oe || [];
      const open_edges = rawOe.map(_normEdge);
      const rawDr = base.doors || base.dr || [];
      const doors = rawDr
        .map((d) => {
          if (Array.isArray(d) && d.length >= 3) {
            return { edge: _normEdge(d[0]), pos: Number(d[1]), width: Number(d[2]) };
          }
          if (d && typeof d === "object" && (d.edge || d.e)) {
            return {
              edge: _normEdge(d.edge || d.e),
              pos: Number(d.pos ?? d.p ?? 0.5),
              width: Number(d.width ?? d.w ?? 0.9),
            };
          }
          return null;
        })
        .filter(Boolean);
      return { ...base, open_edges, doors };
    });
  }

  _getSceneData() {
    const st = this._getStateObj();
    const attrs = (st && st.attributes) || {};
    const geo = attrs.geometry || {};
    const fw = Math.max(1, Number(geo.floor_width_m) || 10);
    const fh = Math.max(1, Number(geo.floor_height_m) || 8);

    const devices = (attrs.devices || []).map((raw) => {
      const d = Array.isArray(raw)
        ? {
            id: String(raw[0] || ""),
            name: String(raw[1] || raw[0] || ""),
            protocol: raw[2] === "b" ? "ble" : "wifi",
            x: Number(raw[3]) || 0,
            y: Number(raw[4]) || 0,
            z: Number(raw[5]) || 1.0,
          }
        : { ...raw };
      const draft = this._draftPositions[d.id];
      return draft ? { ...d, x: draft.x, y: draft.y, z: draft.z } : d;
    });

    const receivers = (attrs.receivers || []).map((r) => {
      const draft = this._draftPositions[r.id];
      return draft ? { ...r, x: draft.x, y: draft.y, z: draft.z } : { ...r };
    });

    const rooms = this._decodeRooms(attrs.rooms_geometry);

    const posLookup = {};
    for (const d of devices) posLookup[d.id] = [d.x, d.y, d.z];
    for (const r of receivers) posLookup[r.id] = [r.x, r.y, r.z];

    const links = (attrs.active_links_3d || []).map((lk) => {
      if (Array.isArray(lk)) {
        const dObj = devices[lk[0]];
        const rObj = receivers[lk[1]];
        const p1 = dObj ? [dObj.x, dObj.y, dObj.z] : [0, 0, 1];
        const p2 = rObj ? [rObj.x, rObj.y, rObj.z] : [fw * 0.5, fh * 0.5, 1];
        return {
          id: `${dObj ? dObj.id : lk[0]}->${rObj ? rObj.id : lk[1]}`,
          p: lk[2] === "b" ? "ble" : "wifi",
          p1,
          p2,
          pert: Number(lk[3]) || 0,
        };
      }
      const parts = String(lk.id || "").split("->");
      const p1 = (parts[0] && posLookup[parts[0]]) || lk.p1 || [0, 0, 1];
      const p2 =
        (parts[1] && posLookup[parts[1]]) || lk.p2 || [fw * 0.5, fh * 0.5, 1];
      return { ...lk, p1, p2 };
    });

    return {
      stateObj: st,
      fw,
      fh,
      devices,
      receivers,
      rooms,
      links,
      targets: attrs.targets || [],
      peaks: attrs.heatmap_peaks || [],
      calibrating: Boolean(attrs.calibrating),
      edgeWalkActive: Boolean(attrs.edge_walk_active),
      fingerprintCount: Number(attrs.fingerprint_count) || 0,
      fingerprintsXY: Array.isArray(attrs.fingerprints_xy)
        ? attrs.fingerprints_xy
        : [],
      maskOutside: attrs.mask_outside_rooms !== false,
    };
  }

  _ensureDom() {
    if (this._domReady) return;
    this._domReady = true;

    const h = this._config.height || 460;
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; height: 100%; }
        ha-card {
          background: var(--ha-card-background, #0f172a);
          color: var(--primary-text-color, #e2e8f0);
          border-radius: var(--ha-card-border-radius, 12px);
          overflow: hidden;
          display: flex;
          flex-direction: column;
          font-family: var(--paper-font-body1_-_font-family, system-ui, -apple-system, sans-serif);
        }
        ha-card.full-panel {
          border-radius: 0;
          height: 100%;
          box-shadow: none;
          border: none;
        }
        .header {
          display: flex;
          flex-wrap: wrap;
          align-items: center;
          justify-content: space-between;
          gap: 8px;
          padding: 10px 14px;
          background: rgba(15, 23, 42, 0.96);
          border-bottom: 1px solid rgba(100, 116, 139, 0.3);
        }
        .title {
          font-size: 15px;
          font-weight: 600;
          color: #f8fafc;
          display: flex;
          align-items: center;
          gap: 8px;
        }
        .pills {
          display: flex;
          flex-wrap: wrap;
          gap: 6px;
          align-items: center;
          font-size: 12px;
        }
        .pill {
          padding: 2px 8px;
          border-radius: 999px;
          background: rgba(30, 41, 59, 0.9);
          border: 1px solid rgba(100, 116, 139, 0.35);
          color: #cbd5e1;
          white-space: nowrap;
        }
        .pill.human { border-color: #10b981; color: #6ee7b7; }
        .pill.animal { border-color: #fb923c; color: #fdba74; }
        .pill.fp { border-color: #34d399; color: #a7f3d0; }
        .pill.cal { border-color: #f59e0b; background: rgba(217,119,6,0.28); color: #fde047; }
        .pill.walk { border-color: #10b981; background: rgba(5,150,105,0.35); color: #6ee7b7; }
        .toolbar {
          display: flex;
          flex-wrap: wrap;
          gap: 6px;
          padding: 8px 12px;
          background: rgba(15, 23, 42, 0.9);
          border-bottom: 1px solid rgba(100, 116, 139, 0.25);
        }
        .sep {
          width: 1px;
          background: rgba(100, 116, 139, 0.35);
          margin: 2px 2px;
        }
        button {
          background: #1e293b;
          color: #e2e8f0;
          border: 1px solid #334155;
          border-radius: 6px;
          padding: 5px 10px;
          font-size: 12px;
          cursor: pointer;
          transition: background 0.15s, border-color 0.15s;
        }
        button:hover { background: #334155; }
        button.active {
          background: #0284c7;
          border-color: #38bdf8;
          color: #ffffff;
        }
        button.warn { border-color: #f59e0b; color: #fde68a; }
        button.success {
          background: #059669;
          border-color: #34d399;
          color: #ffffff;
        }
        button.danger {
          background: #991b1b;
          border-color: #f87171;
          color: #ffffff;
        }
        .viewport {
          position: relative;
          width: 100%;
          height: ${h}px;
          flex: 1 1 auto;
          min-height: 260px;
          background: #090f1d;
          user-select: none;
          touch-action: none;
        }
        canvas {
          width: 100%;
          height: 100%;
          display: block;
          cursor: grab;
        }
        canvas.dragging { cursor: grabbing; }
        canvas.crosshair { cursor: crosshair; }
        .hint {
          position: absolute;
          bottom: 8px;
          left: 10px;
          font-size: 11px;
          color: rgba(203, 213, 225, 0.78);
          background: rgba(15, 23, 42, 0.78);
          padding: 4px 9px;
          border-radius: 6px;
          pointer-events: none;
          border: 1px solid rgba(100, 116, 139, 0.25);
        }
        .toast {
          position: absolute;
          top: 10px;
          right: 10px;
          font-size: 12px;
          color: #f8fafc;
          background: rgba(5, 150, 105, 0.94);
          padding: 6px 12px;
          border-radius: 6px;
          display: none;
          box-shadow: 0 4px 12px rgba(0, 0, 0, 0.35);
        }
        .panel {
          display: none;
          padding: 10px 14px;
          background: #0f172a;
          border-top: 1px solid #334155;
          font-size: 12px;
          color: #cbd5e1;
        }
        .panel.open { display: block; }
        .panel-row {
          display: flex;
          flex-wrap: wrap;
          align-items: center;
          gap: 8px;
          margin-bottom: 8px;
        }
        .panel-row:last-child { margin-bottom: 0; }
        .panel-row label {
          display: inline-flex;
          align-items: center;
          gap: 4px;
        }
        input[type="text"], input[type="number"], select {
          background: #1e293b;
          color: #f8fafc;
          border: 1px solid #475569;
          border-radius: 5px;
          padding: 4px 7px;
          font-size: 12px;
        }
        input[type="number"] { width: 66px; }
        input[type="text"] { width: 112px; }
        input[type="range"] { width: 108px; accent-color: #38bdf8; }
        .badge-info {
          font-size: 11px;
          color: #94a3b8;
        }
      </style>
      <ha-card>
        <div class="header">
          <div class="title" id="card-title"></div>
          <div class="pills" id="hud-pills"></div>
        </div>
        <div class="toolbar">
          <button id="btn-orbit" class="active">3D Orbit</button>
          <button id="btn-iso">Isometrisch</button>
          <button id="btn-2d">Top-Down</button>
          <button id="btn-reset-view">Reset</button>
          <span class="sep"></span>
          <button id="btn-cal-mode">Einmessen / Ablaufen</button>
          <button id="btn-edit-mode">3D Bearbeiten</button>
          <span class="sep"></span>
          <button id="btn-auto-place" title="Geräte & BLE-Scanner automatisch anhand HA-Areas & Namen platzieren">Auto-Zuordnen</button>
          <button id="btn-calibrate" class="warn">Leer-Kalibrierung</button>
          <button id="btn-reset-base">Baseline Reset</button>
        </div>
        <div class="viewport">
          <canvas id="canvas"></canvas>
          <div class="hint" id="hint-text">Links-Ziehen: 360° 3D-Drehen | Rechts/Shift-Ziehen: Verschieben | Scroll/Pinch: Zoom</div>
          <div class="toast" id="toast"></div>
        </div>

        <!-- Integrated Walk-Calibration Wizard (Mobile + Desktop) -->
        <div class="panel" id="cal-panel">
          <div class="panel-row">
            <strong>1. Punkt einmessen:</strong>
            <span class="badge-info" id="cal-pt-label">Auf Boden tippen/klicken oder Raum wählen</span>
            <select id="cal-room-quick"></select>
            <label>Dauer<input type="number" id="cal-dur" min="2" max="20" value="5">s</label>
            <button id="btn-record-pt" class="success">Hier einmessen (5 s)</button>
          </div>
          <div class="panel-row">
            <strong>2. Raumkante ablaufen:</strong>
            <select id="walk-room"></select>
            <select id="walk-edge">
              <option value="north">Nord-Wand (oben)</option>
              <option value="south">Süd-Wand (unten)</option>
              <option value="west">West-Wand (links)</option>
              <option value="east">Ost-Wand (rechts)</option>
            </select>
            <button id="btn-walk-start" class="success">Kante ablaufen starten</button>
            <button id="btn-walk-stop" class="warn">Kante abschließen</button>
            <button id="btn-clear-fp" class="danger">Messpunkte löschen</button>
          </div>
        </div>

        <!-- 3D Edit Mode Panel (Drag & Drop devices, receivers & rooms) -->
        <div class="panel" id="edit-panel">
          <div class="panel-row">
            <strong id="sel-label">Gerät/Empfänger auf der 3D-Karte anklicken & ziehen</strong>
            <label>X<input type="number" id="in-x" step="0.1" min="0" max="50"></label>
            <label>Y<input type="number" id="in-y" step="0.1" min="0" max="50"></label>
            <label>Z<input type="range" id="in-z" min="0" max="3.0" step="0.05"><span id="val-z">1.00m</span></label>
            <button id="btn-save-pos" class="success">Position speichern</button>
          </div>
          <div class="panel-row">
            <strong>Raum:</strong>
            <input type="text" id="rm-id" placeholder="raum_id">
            <input type="text" id="rm-name" placeholder="Name">
            <label>X1<input type="number" id="rm-x1" step="0.1" value="0"></label>
            <label>Y1<input type="number" id="rm-y1" step="0.1" value="0"></label>
            <label>X2<input type="number" id="rm-x2" step="0.1" value="5"></label>
            <label>Y2<input type="number" id="rm-y2" step="0.1" value="4"></label>
            <label>Offen:
              <input type="checkbox" id="oe-n">N
              <input type="checkbox" id="oe-e">O
              <input type="checkbox" id="oe-s">S
              <input type="checkbox" id="oe-w">W
            </label>
            <button id="btn-save-room" class="success">Raum speichern</button>
            <button id="btn-del-room" class="danger">Löschen</button>
          </div>
        </div>
      </ha-card>
    `;

    this._canvas = this.shadowRoot.getElementById("canvas");
    this._ctx = this._canvas.getContext("2d");
    this._bindEvents();
    this._updateHud();
    this._populateRoomDropdowns();
  }

  _showToast(msg) {
    const el = this.shadowRoot && this.shadowRoot.getElementById("toast");
    if (!el) return;
    el.textContent = String(msg);
    el.style.display = "block";
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => {
      el.style.display = "none";
    }, 2600);
  }

  _makePill(text, cls) {
    const span = document.createElement("span");
    span.className = cls ? `pill ${cls}` : "pill";
    span.textContent = text;
    return span;
  }

  _updateHud() {
    if (!this.shadowRoot) return;
    const titleEl = this.shadowRoot.getElementById("card-title");
    if (titleEl) {
      titleEl.textContent = this._config.title || "RF Presence Sense 3D";
    }
    const pills = this.shadowRoot.getElementById("hud-pills");
    if (!pills) return;

    const data = this._getSceneData();
    const humans = data.targets.filter((t) => t.type === "human").length;
    const animals = data.targets.filter((t) => t.type !== "human").length;
    const activePert = data.links.filter((l) => (l.pert || 0) > 1.2).length;

    pills.replaceChildren();
    pills.appendChild(this._makePill(`Menschen: ${humans}`, "human"));
    pills.appendChild(this._makePill(`Tiere: ${animals}`, "animal"));
    pills.appendChild(
      this._makePill(`Links: ${data.links.length} (${activePert} aktiv)`, "")
    );
    pills.appendChild(
      this._makePill(`Messpunkte: ${data.fingerprintCount}`, "fp")
    );
    if (data.calibrating) {
      pills.appendChild(this._makePill("Leer-Kalibrierung...", "cal"));
    }
    if (data.edgeWalkActive) {
      pills.appendChild(this._makePill("Kante wird abgelaufen...", "walk"));
    }
  }

  _populateRoomDropdowns() {
    if (!this.shadowRoot) return;
    const data = this._getSceneData();
    const calQuick = this.shadowRoot.getElementById("cal-room-quick");
    const walkRoom = this.shadowRoot.getElementById("walk-room");
    if (!calQuick || !walkRoom) return;

    const sig = data.rooms.map((r) => `${r.id}:${r.name || ""}`).join("|");
    if (sig === this._lastRoomSig && walkRoom.options.length > 0) {
      this._updateWalkPreview();
      return;
    }
    this._lastRoomSig = sig;

    const prevCal = calQuick.value;
    const prevWalk = walkRoom.value;

    calQuick.replaceChildren();
    const defOpt = document.createElement("option");
    defOpt.value = "";
    defOpt.textContent = "— Raum-Mitte wählen —";
    calQuick.appendChild(defOpt);

    walkRoom.replaceChildren();
    for (const rm of data.rooms) {
      const o1 = document.createElement("option");
      o1.value = rm.id;
      o1.textContent = rm.name || rm.id;
      calQuick.appendChild(o1);

      const o2 = document.createElement("option");
      o2.value = rm.id;
      o2.textContent = rm.name || rm.id;
      walkRoom.appendChild(o2);
    }

    if (prevCal) calQuick.value = prevCal;
    if (prevWalk && data.rooms.some((r) => r.id === prevWalk)) {
      walkRoom.value = prevWalk;
    }
    this._updateWalkPreview();
  }

  _getEdgeSegment(room, edge) {
    const inset = 0.25;
    const x0 = room.x_min + inset;
    const x1 = Math.max(x0, room.x_max - inset);
    const y0 = room.y_min + inset;
    const y1 = Math.max(y0, room.y_max - inset);
    if (edge === "north") return { sx: x0, sy: y0, ex: x1, ey: y0 };
    if (edge === "south") return { sx: x0, sy: y1, ex: x1, ey: y1 };
    if (edge === "west") return { sx: x0, sy: y0, ex: x0, ey: y1 };
    return { sx: x1, sy: y0, ex: x1, ey: y1 }; // east
  }

  _updateWalkPreview() {
    if (this._mode !== "calibrate" || !this.shadowRoot) {
      this._walkPreview = null;
      return;
    }
    const rid = this.shadowRoot.getElementById("walk-room").value;
    const edge = this.shadowRoot.getElementById("walk-edge").value;
    const data = this._getSceneData();
    const room = data.rooms.find((r) => r.id === rid);
    this._walkPreview = room ? this._getEdgeSegment(room, edge) : null;
  }

  _setMode(mode) {
    const $ = (id) => this.shadowRoot.getElementById(id);
    this._mode = this._mode === mode ? "view" : mode;
    $("btn-cal-mode").classList.toggle("active", this._mode === "calibrate");
    $("btn-edit-mode").classList.toggle("active", this._mode === "edit");
    $("cal-panel").classList.toggle("open", this._mode === "calibrate");
    $("edit-panel").classList.toggle("open", this._mode === "edit");
    this._canvas.classList.toggle("crosshair", this._mode !== "view");

    if (this._mode === "calibrate") {
      this._updateWalkPreview();
      $("hint-text").textContent =
        "Einmess-Modus: Auf 3D-Boden tippen/klicken für Messpunkt | Ziehen: 3D-Drehen";
    } else if (this._mode === "edit") {
      this._walkPreview = null;
      $("hint-text").textContent =
        "3D-Edit: Geräte/Empfänger/Raum-Ecken ziehen | Leerer Raum: 3D-Drehen";
    } else {
      this._walkPreview = null;
      this._draftPositions = {};
      this._draftRooms = {};
      this._selectedItem = null;
      $("hint-text").textContent =
        "Links-Ziehen: 360° 3D-Drehen | Rechts/Shift-Ziehen: Verschieben | Scroll/Pinch: Zoom";
    }
    this._renderScene();
  }

  _setCameraPreset(preset) {
    const $ = (id) => this.shadowRoot.getElementById(id);
    this._velYaw = 0;
    this._velPitch = 0;
    if (preset === "orbit") {
      this._yaw = (38 * Math.PI) / 180;
      this._pitch = (34 * Math.PI) / 180;
    } else if (preset === "iso") {
      this._yaw = (45 * Math.PI) / 180;
      this._pitch = (35.26 * Math.PI) / 180;
    } else if (preset === "top") {
      this._yaw = 0;
      this._pitch = (88 * Math.PI) / 180;
    } else {
      this._yaw = (38 * Math.PI) / 180;
      this._pitch = (34 * Math.PI) / 180;
      this._zoom = 1.0;
      this._panX = 0;
      this._panY = 0;
    }
    $("btn-orbit").classList.toggle("active", preset === "orbit" || preset === "reset");
    $("btn-iso").classList.toggle("active", preset === "iso");
    $("btn-2d").classList.toggle("active", preset === "top");
    this._renderScene();
  }

  _bindEvents() {
    const $ = (id) => this.shadowRoot.getElementById(id);

    $("btn-orbit").addEventListener("click", () => this._setCameraPreset("orbit"));
    $("btn-iso").addEventListener("click", () => this._setCameraPreset("iso"));
    $("btn-2d").addEventListener("click", () => this._setCameraPreset("top"));
    $("btn-reset-view").addEventListener("click", () => this._setCameraPreset("reset"));

    $("btn-cal-mode").addEventListener("click", () => this._setMode("calibrate"));
    $("btn-edit-mode").addEventListener("click", () => this._setMode("edit"));

    $("btn-auto-place").addEventListener("click", () => {
      if (!this._hass) return;
      this._hass.callService("rf_presence_sense", "auto_place_from_areas", {});
      this._showToast("Geräte & BLE-Scanner automatisch den Räumen zugeordnet!");
    });

    $("btn-calibrate").addEventListener("click", () => {
      if (!this._hass) return;
      this._hass.callService("rf_presence_sense", "calibrate_empty_room", {});
      this._showToast("Leer-Raum-Kalibrierung gestartet...");
    });

    $("btn-reset-base").addEventListener("click", () => {
      if (!this._hass) return;
      this._hass.callService("rf_presence_sense", "reset_baseline", {});
      this._showToast("Baseline zurückgesetzt!");
    });

    // Calibration Wizard controls
    $("cal-room-quick").addEventListener("change", () => {
      const rid = $("cal-room-quick").value;
      if (!rid) return;
      const rm = this._getSceneData().rooms.find((r) => r.id === rid);
      if (!rm) return;
      const cx = Math.round(((rm.x_min + rm.x_max) * 0.5) * 100) / 100;
      const cy = Math.round(((rm.y_min + rm.y_max) * 0.5) * 100) / 100;
      this._calPoint = { x: cx, y: cy, room_id: rm.id };
      $("cal-pt-label").textContent = `${rm.name} (${cx.toFixed(1)}m, ${cy.toFixed(1)}m)`;
      this._renderScene();
    });

    $("cal-dur").addEventListener("input", () => {
      const d = Math.max(2, Math.min(20, Number($("cal-dur").value) || 5));
      $("btn-record-pt").textContent = `Hier einmessen (${d} s)`;
    });

    $("btn-record-pt").addEventListener("click", () => {
      if (!this._hass) return;
      if (!this._calPoint) {
        this._showToast("Erst auf den 3D-Boden tippen oder Raum wählen!");
        return;
      }
      const dur = Math.max(2, Math.min(20, Number($("cal-dur").value) || 5));
      this._hass.callService("rf_presence_sense", "record_calibration_point", {
        x: this._calPoint.x,
        y: this._calPoint.y,
        room_id: this._calPoint.room_id || "",
        duration_seconds: dur,
      });
      this._showToast(
        `Messpunkt (${this._calPoint.x.toFixed(1)}m, ${this._calPoint.y.toFixed(1)}m) wird ${dur}s eingemessen...`
      );
    });

    $("walk-room").addEventListener("change", () => {
      this._updateWalkPreview();
      this._renderScene();
    });
    $("walk-edge").addEventListener("change", () => {
      this._updateWalkPreview();
      this._renderScene();
    });

    $("btn-walk-start").addEventListener("click", () => {
      if (!this._hass) return;
      const rid = $("walk-room").value;
      const edge = $("walk-edge").value;
      const rm = this._getSceneData().rooms.find((r) => r.id === rid);
      if (!rm) {
        this._showToast("Bitte zuerst einen Raum auswählen");
        return;
      }
      const seg = this._getEdgeSegment(rm, edge);
      this._walkPreview = seg;
      this._hass.callService("rf_presence_sense", "start_edge_walk", {
        room_id: rm.id,
        start_x: Math.round(seg.sx * 100) / 100,
        start_y: Math.round(seg.sy * 100) / 100,
        end_x: Math.round(seg.ex * 100) / 100,
        end_y: Math.round(seg.ey * 100) / 100,
      });
      this._showToast(`Edge-Walk gestartet (${rm.name} — ${edge}). Langsam abgehen!`);
      this._renderScene();
    });

    $("btn-walk-stop").addEventListener("click", () => {
      if (!this._hass) return;
      this._hass.callService("rf_presence_sense", "stop_edge_walk", {});
      this._showToast("Edge-Walk abgeschlossen & Fingerprints gespeichert!");
    });

    $("btn-clear-fp").addEventListener("click", () => {
      if (!this._hass) return;
      this._hass.callService("rf_presence_sense", "clear_fingerprints", {});
      this._showToast("Alle Kalibrierungs-Messpunkte gelöscht.");
    });

    // Edit Mode inputs
    const syncPosInputs = () => {
      if (!this._selectedItem || this._selectedItem.kind === "room_corner") return;
      const id = this._selectedItem.id;
      const x = Math.max(0, Number($("in-x").value) || 0);
      const y = Math.max(0, Number($("in-y").value) || 0);
      const z = Math.max(0, Math.min(3.5, Number($("in-z").value) || 0));
      $("val-z").textContent = `${z.toFixed(2)}m`;
      this._draftPositions[id] = { x, y, z, name: this._selectedItem.name };
      this._renderScene();
    };
    $("in-x").addEventListener("input", syncPosInputs);
    $("in-y").addEventListener("input", syncPosInputs);
    $("in-z").addEventListener("input", syncPosInputs);

    $("btn-save-pos").addEventListener("click", () => {
      if (!this._hass || !this._selectedItem || this._selectedItem.kind === "room_corner") {
        this._showToast("Zuerst ein Gerät oder Empfänger auswählen");
        return;
      }
      const id = this._selectedItem.id;
      const x = Number($("in-x").value) || 0;
      const y = Number($("in-y").value) || 0;
      const z = Number($("in-z").value) || 1.0;
      this._hass.callService("rf_presence_sense", "set_device_position", {
        device_id: id,
        x: Math.round(x * 100) / 100,
        y: Math.round(y * 100) / 100,
        z: Math.round(z * 100) / 100,
      });
      delete this._draftPositions[id];
      this._showToast(`Gespeichert: ${this._selectedItem.name}`);
    });

    $("btn-save-room").addEventListener("click", () => {
      if (!this._hass) return;
      const room_id = ($("rm-id").value || "").trim();
      const name = ($("rm-name").value || "").trim() || room_id;
      if (!room_id) {
        this._showToast("Bitte Raum-ID eingeben");
        return;
      }
      const open_edges = [];
      if ($("oe-n").checked) open_edges.push("north");
      if ($("oe-e").checked) open_edges.push("east");
      if ($("oe-s").checked) open_edges.push("south");
      if ($("oe-w").checked) open_edges.push("west");

      const existingRoom = this._getSceneData().rooms.find(
        (r) => r.id === room_id
      );
      const serviceData = {
        room_id,
        name,
        x_min: Number($("rm-x1").value) || 0,
        y_min: Number($("rm-y1").value) || 0,
        x_max: Number($("rm-x2").value) || 5,
        y_max: Number($("rm-y2").value) || 4,
        open_edges,
      };
      if (existingRoom && Array.isArray(existingRoom.doors) && existingRoom.doors.length) {
        serviceData.doors = existingRoom.doors;
      }

      this._hass.callService("rf_presence_sense", "define_room", serviceData);
      delete this._draftRooms[room_id];
      this._showToast(`Raum "${name}" gespeichert`);
    });

    $("btn-del-room").addEventListener("click", () => {
      if (!this._hass) return;
      const room_id = ($("rm-id").value || "").trim();
      if (!room_id) {
        this._showToast("Bitte Raum-ID eingeben");
        return;
      }
      this._hass.callService("rf_presence_sense", "delete_room", { room_id });
      delete this._draftRooms[room_id];
      this._showToast(`Raum "${room_id}" gelöscht`);
    });

    // Pointer / Touch / Wheel 3D CAD Navigation
    const canvas = this._canvas;
    canvas.addEventListener("contextmenu", (ev) => ev.preventDefault());
    canvas.addEventListener("pointerdown", (ev) => this._onPointerDown(ev));
    canvas.addEventListener(
      "wheel",
      (ev) => {
        ev.preventDefault();
        const factor = ev.deltaY > 0 ? 0.91 : 1.1;
        this._zoom = Math.max(0.45, Math.min(3.5, this._zoom * factor));
        this._renderScene();
      },
      { passive: false }
    );
  }

  _buildCamera(w, h, fw, fh) {
    const cx = fw * 0.5;
    const cy = fh * 0.5;
    const cz = 0.85;
    const cosY = Math.cos(this._yaw);
    const sinY = Math.sin(this._yaw);
    const cosP = Math.cos(this._pitch);
    const sinP = Math.sin(this._pitch);
    const diag = Math.hypot(fw, fh, 2.6);
    const baseScale = (Math.min(w, h) * 0.74 * this._zoom) / Math.max(diag, 1);
    const originX = w * 0.5 + this._panX;
    const originY = h * 0.54 + this._panY;

    const project = (x, y, z) => {
      const dx = x - cx;
      const dy = y - cy;
      const dz = z - cz;
      const rx = dx * cosY - dy * sinY;
      const ry = dx * sinY + dy * cosY;
      const sx = originX + rx * baseScale;
      const sy = originY + (ry * sinP - dz * cosP) * baseScale;
      // Camera-space depth (smaller = further away, larger = closer to viewer)
      const depth = ry * cosP + dz * sinP;
      return { x: sx, y: sy, depth };
    };

    const unprojectToPlane = (px, py, planeZ) => {
      const dz = planeZ - cz;
      const rx = (px - originX) / baseScale;
      const ry =
        Math.abs(sinP) > 0.08
          ? ((py - originY) / baseScale + dz * cosP) / sinP
          : 0;
      const dx = rx * cosY + ry * sinY;
      const dy = -rx * sinY + ry * cosY;
      return {
        x: Math.max(0, Math.min(fw, cx + dx)),
        y: Math.max(0, Math.min(fh, cy + dy)),
      };
    };

    return { project, unprojectToPlane, baseScale };
  }

  _hitTestEditHandle(px, py, data, cam) {
    const threshold = 16;
    let best = null;
    let bestDist = threshold;

    for (const r of data.receivers) {
      const p = cam.project(r.x, r.y, r.z);
      const d = Math.hypot(p.x - px, p.y - py);
      if (d < bestDist) {
        bestDist = d;
        best = { kind: "receiver", id: r.id, name: r.name, x: r.x, y: r.y, z: r.z };
      }
    }
    for (const dev of data.devices) {
      const p = cam.project(dev.x, dev.y, dev.z);
      const d = Math.hypot(p.x - px, p.y - py);
      if (d < bestDist) {
        bestDist = d;
        best = {
          kind: "device",
          id: dev.id,
          name: dev.name,
          x: dev.x,
          y: dev.y,
          z: dev.z,
        };
      }
    }
    for (const rm of data.rooms) {
      if (rm.polygon && rm.polygon.length >= 3) continue;
      for (const c of [
        { key: "min", x: rm.x_min, y: rm.y_min },
        { key: "max", x: rm.x_max, y: rm.y_max },
      ]) {
        const p = cam.project(c.x, c.y, 0.05);
        const d = Math.hypot(p.x - px, p.y - py);
        if (d < bestDist) {
          bestDist = d;
          best = {
            kind: "room_corner",
            id: rm.id,
            name: rm.name,
            corner: c.key,
            room: { ...rm },
            z: 0.05,
          };
        }
      }
    }
    return best;
  }

  _populateEditorForSelection(sel) {
    const $ = (id) => this.shadowRoot.getElementById(id);
    if (!sel) return;
    if (sel.kind === "room_corner") {
      const rm = sel.room;
      $("rm-id").value = rm.id;
      $("rm-name").value = rm.name;
      $("rm-x1").value = Number(rm.x_min).toFixed(1);
      $("rm-y1").value = Number(rm.y_min).toFixed(1);
      $("rm-x2").value = Number(rm.x_max).toFixed(1);
      $("rm-y2").value = Number(rm.y_max).toFixed(1);
      const oe = new Set(rm.open_edges || []);
      $("oe-n").checked = oe.has("north");
      $("oe-e").checked = oe.has("east");
      $("oe-s").checked = oe.has("south");
      $("oe-w").checked = oe.has("west");
    } else {
      $("sel-label").textContent = `${sel.kind === "receiver" ? "[RX] " : ""}${sel.name}`;
      $("in-x").value = Number(sel.x).toFixed(2);
      $("in-y").value = Number(sel.y).toFixed(2);
      $("in-z").value = Number(sel.z).toFixed(2);
      $("val-z").textContent = `${Number(sel.z).toFixed(2)}m`;
    }
  }

  _onPointerDown(ev) {
    this._velYaw = 0;
    this._velPitch = 0;
    this._activeTouches.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });

    // 2-finger pinch / pan on touchscreens
    if (this._activeTouches.size === 2) {
      const pts = Array.from(this._activeTouches.values());
      this._pinchDist = Math.hypot(pts[1].x - pts[0].x, pts[1].y - pts[0].y);
      this._pinchCenter = {
        x: (pts[0].x + pts[1].x) * 0.5,
        y: (pts[0].y + pts[1].y) * 0.5,
      };
      this._dragKind = "pinch";
      return;
    }

    const rect = this._canvas.getBoundingClientRect();
    const px = ev.clientX - rect.left;
    const py = ev.clientY - rect.top;
    this._lastPtr = { x: ev.clientX, y: ev.clientY };
    this._downPtr = { x: ev.clientX, y: ev.clientY };
    this._movedSinceDown = false;

    // Right button or Shift+Left -> 3D Pan
    if (ev.button === 2 || (ev.button === 0 && ev.shiftKey)) {
      this._dragKind = "pan";
      this._canvas.classList.add("dragging");
      return;
    }

    if (this._mode === "edit" && ev.button === 0) {
      const data = this._getSceneData();
      const cam = this._buildCamera(rect.width, rect.height, data.fw, data.fh);
      const hit = this._hitTestEditHandle(px, py, data, cam);
      if (hit) {
        this._selectedItem = hit;
        this._draggingItem = hit;
        this._dragKind = "item";
        this._populateEditorForSelection(hit);
        this._canvas.classList.add("dragging");
        this._renderScene();
        return;
      }
    }

    this._dragKind = "orbit";
    this._canvas.classList.add("dragging");
  }

  _onPointerMove(ev) {
    if (this._activeTouches.has(ev.pointerId)) {
      this._activeTouches.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
    }

    if (this._dragKind === "pinch" && this._activeTouches.size === 2) {
      const pts = Array.from(this._activeTouches.values());
      const dist = Math.hypot(pts[1].x - pts[0].x, pts[1].y - pts[0].y);
      const center = {
        x: (pts[0].x + pts[1].x) * 0.5,
        y: (pts[0].y + pts[1].y) * 0.5,
      };
      if (this._pinchDist > 10) {
        this._zoom = Math.max(
          0.45,
          Math.min(3.5, this._zoom * (dist / this._pinchDist))
        );
      }
      if (this._pinchCenter) {
        this._panX += center.x - this._pinchCenter.x;
        this._panY += center.y - this._pinchCenter.y;
      }
      this._pinchDist = dist;
      this._pinchCenter = center;
      this._renderScene();
      return;
    }

    if (!this._dragKind) return;

    if (
      this._downPtr &&
      Math.hypot(ev.clientX - this._downPtr.x, ev.clientY - this._downPtr.y) > 4
    ) {
      this._movedSinceDown = true;
    }

    const rect = this._canvas.getBoundingClientRect();

    if (this._dragKind === "item" && this._draggingItem) {
      const px = ev.clientX - rect.left;
      const py = ev.clientY - rect.top;
      const data = this._getSceneData();
      const cam = this._buildCamera(rect.width, rect.height, data.fw, data.fh);
      const pt = cam.unprojectToPlane(px, py, this._draggingItem.z || 0.05);

      if (this._draggingItem.kind === "room_corner") {
        const rm = {
          ...(this._draftRooms[this._draggingItem.id] || this._draggingItem.room),
        };
        if (this._draggingItem.corner === "min") {
          rm.x_min = Math.round(pt.x * 10) / 10;
          rm.y_min = Math.round(pt.y * 10) / 10;
        } else {
          rm.x_max = Math.round(pt.x * 10) / 10;
          rm.y_max = Math.round(pt.y * 10) / 10;
        }
        this._draftRooms[rm.id] = rm;
        this._draggingItem.room = rm;
        this._populateEditorForSelection(this._draggingItem);
      } else {
        const id = this._draggingItem.id;
        const nextPos = {
          x: Math.round(pt.x * 20) / 20,
          y: Math.round(pt.y * 20) / 20,
          z: this._draggingItem.z,
          name: this._draggingItem.name,
        };
        this._draftPositions[id] = nextPos;
        this._draggingItem.x = nextPos.x;
        this._draggingItem.y = nextPos.y;
        this._populateEditorForSelection(this._draggingItem);
      }
      this._renderScene();
      return;
    }

    const dx = ev.clientX - this._lastPtr.x;
    const dy = ev.clientY - this._lastPtr.y;
    this._lastPtr = { x: ev.clientX, y: ev.clientY };

    if (this._dragKind === "pan") {
      this._panX += dx;
      this._panY += dy;
      this._renderScene();
      return;
    }

    if (this._dragKind === "orbit") {
      const dYaw = dx * 0.0085;
      const dPitch = dy * 0.0075;
      this._yaw += dYaw;
      this._pitch = Math.max(
        (8 * Math.PI) / 180,
        Math.min((88 * Math.PI) / 180, this._pitch + dPitch)
      );
      this._velYaw = dYaw * 0.75;
      this._velPitch = dPitch * 0.75;
      this._renderScene();
    }
  }

  _onPointerUp(ev) {
    if (ev && ev.pointerId !== undefined) {
      this._activeTouches.delete(ev.pointerId);
    }

    // Tap/click on 3D floor in Calibration Mode -> place calibration crosshair
    if (
      this._mode === "calibrate" &&
      this._dragKind === "orbit" &&
      !this._movedSinceDown &&
      ev
    ) {
      const rect = this._canvas.getBoundingClientRect();
      const px = ev.clientX - rect.left;
      const py = ev.clientY - rect.top;
      const data = this._getSceneData();
      const cam = this._buildCamera(rect.width, rect.height, data.fw, data.fh);
      const pt = cam.unprojectToPlane(px, py, 0.0);
      const cx = Math.round(pt.x * 20) / 20;
      const cy = Math.round(pt.y * 20) / 20;
      const matched = data.rooms.find(
        (r) => cx >= r.x_min && cx <= r.x_max && cy >= r.y_min && cy <= r.y_max
      );
      this._calPoint = { x: cx, y: cy, room_id: matched ? matched.id : "" };
      const lbl = this.shadowRoot.getElementById("cal-pt-label");
      if (lbl) {
        lbl.textContent = `${matched ? matched.name + " " : ""}(${cx.toFixed(2)}m, ${cy.toFixed(2)}m)`;
      }
      this._velYaw = 0;
      this._velPitch = 0;
      this._renderScene();
    }

    const wasOrbit = this._dragKind === "orbit" && this._movedSinceDown;
    this._dragKind = null;
    this._draggingItem = null;
    if (this._canvas) this._canvas.classList.remove("dragging");
    if (wasOrbit) {
      this._syncAnimLoop();
    }
  }

  _drawWallQuad(ctx, P, x0, y0, x1, y1, z0, z1, fill, stroke) {
    const a = P(x0, y0, z0);
    const b = P(x1, y1, z0);
    const c = P(x1, y1, z1);
    const d = P(x0, y0, z1);
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.lineTo(c.x, c.y);
    ctx.lineTo(d.x, d.y);
    ctx.closePath();
    ctx.fillStyle = fill;
    ctx.fill();
    ctx.strokeStyle = stroke;
    ctx.lineWidth = 1;
    ctx.stroke();
  }

  _renderScene() {
    if (!this._canvas || !this._ctx) return;
    const rect = this._canvas.getBoundingClientRect();
    if (rect.width < 10 || rect.height < 10) return;

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.round(rect.width);
    const h = Math.round(rect.height);
    if (this._canvas.width !== w * dpr || this._canvas.height !== h * dpr) {
      this._canvas.width = w * dpr;
      this._canvas.height = h * dpr;
    }

    const ctx = this._ctx;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    // Architectural studio backdrop
    const grad = ctx.createRadialGradient(
      w * 0.5,
      h * 0.45,
      20,
      w * 0.5,
      h * 0.5,
      Math.max(w, h) * 0.72
    );
    grad.addColorStop(0, "#132038");
    grad.addColorStop(1, "#070c17");
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, w, h);

    const data = this._getSceneData();
    const { fw, fh } = data;
    const cam = this._buildCamera(w, h, fw, fh);
    const P = cam.project;
    const pulse = 0.5 + 0.5 * Math.sin(performance.now() * 0.0045);

    const hasCustomRooms = data.rooms.length > 0;
    const roomColors = [
      "#38bdf8",
      "#818cf8",
      "#34d399",
      "#fbbf24",
      "#f472b6",
      "#22d3ee",
    ];
    const wallH = 2.4;
    const doorH = 2.05;
    const slabD = -0.18;

    // 1. Subtle outer bounding frame (floorplan_image projected if configured)
    const c00 = P(0, 0, 0);
    const cw0 = P(fw, 0, 0);
    const cwh = P(fw, fh, 0);
    const c0h = P(0, fh, 0);

    if (!hasCustomRooms || !data.maskOutside) {
      ctx.beginPath();
      ctx.moveTo(c00.x, c00.y);
      ctx.lineTo(cw0.x, cw0.y);
      ctx.lineTo(cwh.x, cwh.y);
      ctx.lineTo(c0h.x, c0h.y);
      ctx.closePath();
      ctx.fillStyle = "#1a2538";
      ctx.fill();
    }
    ctx.beginPath();
    ctx.moveTo(c00.x, c00.y);
    ctx.lineTo(cw0.x, cw0.y);
    ctx.lineTo(cwh.x, cwh.y);
    ctx.lineTo(c0h.x, c0h.y);
    ctx.closePath();
    ctx.setLineDash([4, 6]);
    ctx.strokeStyle = "rgba(100, 116, 139, 0.25)";
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.setLineDash([]);

    if (this._floorImg && this._floorImg.naturalWidth > 0) {
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(c00.x, c00.y);
      ctx.lineTo(cw0.x, cw0.y);
      ctx.lineTo(cwh.x, cwh.y);
      ctx.lineTo(c0h.x, c0h.y);
      ctx.closePath();
      ctx.clip();
      const iw = this._floorImg.naturalWidth;
      const ih = this._floorImg.naturalHeight;
      ctx.globalAlpha = 0.5;
      ctx.transform(
        (cw0.x - c00.x) / iw,
        (cw0.y - c00.y) / iw,
        (c0h.x - c00.x) / ih,
        (c0h.y - c00.y) / ih,
        c00.x,
        c00.y
      );
      ctx.drawImage(this._floorImg, 0, 0);
      ctx.restore();
    }

    // 2. Solid 3D Room Floor Slabs (ONLY inside configured rooms -> neighbor area stays clean!)
    const wallPanels = [];
    const rectEdgeNames = ["north", "east", "south", "west"];

    data.rooms.forEach((rm, idx) => {
      const col = roomColors[idx % roomColors.length];
      const isPoly = Array.isArray(rm.polygon) && rm.polygon.length >= 3;
      const verts = isPoly
        ? rm.polygon
        : [
            [rm.x_min, rm.y_min],
            [rm.x_max, rm.y_min],
            [rm.x_max, rm.y_max],
            [rm.x_min, rm.y_max],
          ];
      const floorPts = verts.map(([vx, vy]) => P(vx, vy, 0.0));
      const botPts = verts.map(([vx, vy]) => P(vx, vy, slabD));

      // Extruded 3D slab sides (for tactile 3D architectural model look)
      for (let i = 0; i < verts.length; i++) {
        const j = (i + 1) % verts.length;
        ctx.beginPath();
        ctx.moveTo(floorPts[i].x, floorPts[i].y);
        ctx.lineTo(floorPts[j].x, floorPts[j].y);
        ctx.lineTo(botPts[j].x, botPts[j].y);
        ctx.lineTo(botPts[i].x, botPts[i].y);
        ctx.closePath();
        ctx.fillStyle = "#111a2c";
        ctx.fill();
      }

      // Solid room floor surface + subtle room tint
      ctx.beginPath();
      floorPts.forEach((p, i) =>
        i === 0 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y)
      );
      ctx.closePath();
      ctx.fillStyle = "#1e293b";
      ctx.fill();
      ctx.fillStyle = col + "1c";
      ctx.fill();

      // Subtle 1m interior floor grid clipped to room
      ctx.save();
      ctx.clip();
      ctx.strokeStyle = "rgba(148, 163, 184, 0.14)";
      ctx.lineWidth = 1;
      for (let gx = Math.ceil(rm.x_min); gx < rm.x_max; gx += 1) {
        const a = P(gx, rm.y_min, 0.005);
        const b = P(gx, rm.y_max, 0.005);
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
      }
      for (let gy = Math.ceil(rm.y_min); gy < rm.y_max; gy += 1) {
        const a = P(rm.x_min, gy, 0.005);
        const b = P(rm.x_max, gy, 0.005);
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
      }
      ctx.restore();

      // Queue 3D translucent wall panels (with open_edges & door cutouts!) for depth sorting
      const openSet = new Set(rm.open_edges || []);
      const doorsByEdge = {};
      for (const dr of rm.doors || []) {
        (doorsByEdge[dr.edge] = doorsByEdge[dr.edge] || []).push(dr);
      }

      for (let i = 0; i < verts.length; i++) {
        const j = (i + 1) % verts.length;
        const [x0, y0] = verts[i];
        const [x1, y1] = verts[j];
        const edgeName = !isPoly && verts.length === 4 ? rectEdgeNames[i] : "";
        if (edgeName && openSet.has(edgeName)) {
          // Open passage: faint dashed threshold line only
          ctx.setLineDash([4, 5]);
          ctx.strokeStyle = col + "44";
          ctx.beginPath();
          ctx.moveTo(floorPts[i].x, floorPts[i].y);
          ctx.lineTo(floorPts[j].x, floorPts[j].y);
          ctx.stroke();
          ctx.setLineDash([]);
          continue;
        }
        const mid = P((x0 + x1) * 0.5, (y0 + y1) * 0.5, wallH * 0.5);
        wallPanels.push({
          depth: mid.depth,
          x0,
          y0,
          x1,
          y1,
          col,
          doors: (edgeName && doorsByEdge[edgeName]) || [],
        });
      }

      // Room floor label
      const cx = verts.reduce((s, v) => s + v[0], 0) / verts.length;
      const cy = verts.reduce((s, v) => s + v[1], 0) / verts.length;
      const lp = P(cx, cy, 0.03);
      ctx.font = "600 11px system-ui, sans-serif";
      ctx.fillStyle = "#e2e8f0";
      ctx.textAlign = "center";
      ctx.fillText(rm.name || rm.id, lp.x, lp.y);

      // Edit handles on room corners
      if (this._mode === "edit" && !isPoly) {
        for (const [cxm, cym] of [
          [rm.x_min, rm.y_min],
          [rm.x_max, rm.y_max],
        ]) {
          const hp = P(cxm, cym, 0.05);
          ctx.fillStyle = col;
          ctx.fillRect(hp.x - 5, hp.y - 5, 10, 10);
          ctx.strokeStyle = "#ffffff";
          ctx.lineWidth = 1.2;
          ctx.strokeRect(hp.x - 5, hp.y - 5, 10, 10);
        }
      }
    });
    ctx.textAlign = "start";

    // Z reference bar
    const z0 = P(0, 0, 0);
    const z2 = P(0, 0, wallH);
    ctx.strokeStyle = "rgba(148, 163, 184, 0.45)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(z0.x, z0.y);
    ctx.lineTo(z2.x, z2.y);
    ctx.stroke();
    ctx.fillStyle = "#94a3b8";
    ctx.font = "10px system-ui, sans-serif";
    ctx.fillText(`${wallH.toFixed(1)}m`, z2.x + 4, z2.y + 3);

    // 3. Calibration Fingerprints on floor (green 3D diamonds)
    for (const fp of data.fingerprintsXY) {
      const p = P(Number(fp[0]) || 0, Number(fp[1]) || 0, 0.03);
      ctx.beginPath();
      ctx.moveTo(p.x, p.y - 4);
      ctx.lineTo(p.x + 5, p.y);
      ctx.lineTo(p.x, p.y + 4);
      ctx.lineTo(p.x - 5, p.y);
      ctx.closePath();
      ctx.fillStyle = "rgba(52, 211, 153, 0.85)";
      ctx.fill();
      ctx.strokeStyle = "#064e3b";
      ctx.lineWidth = 1;
      ctx.stroke();
    }

    // Calibration crosshair reticle & Edge-Walk glowing path preview
    if (this._mode === "calibrate" && this._calPoint) {
      const cp = P(this._calPoint.x, this._calPoint.y, 0.05);
      ctx.beginPath();
      ctx.arc(cp.x, cp.y, 10, 0, Math.PI * 2);
      ctx.strokeStyle = "#22d3ee";
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(cp.x - 14, cp.y);
      ctx.lineTo(cp.x + 14, cp.y);
      ctx.moveTo(cp.x, cp.y - 14);
      ctx.lineTo(cp.x, cp.y + 14);
      ctx.stroke();
    }
    if (this._walkPreview) {
      const a = P(this._walkPreview.sx, this._walkPreview.sy, 0.08);
      const b = P(this._walkPreview.ex, this._walkPreview.ey, 0.08);
      ctx.strokeStyle = data.edgeWalkActive ? "#34d399" : "#fde047";
      ctx.lineWidth = 3.5;
      ctx.setLineDash([8, 6]);
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = "#34d399";
      ctx.beginPath();
      ctx.arc(a.x, a.y, 5, 0, Math.PI * 2);
      ctx.arc(b.x, b.y, 5, 0, Math.PI * 2);
      ctx.fill();
    }

    // 4. True 3D RF Links
    for (const lk of data.links) {
      const a = P(lk.p1[0], lk.p1[1], lk.p1[2]);
      const b = P(lk.p2[0], lk.p2[1], lk.p2[2]);
      const pert = Number(lk.pert) || 0;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      if (pert > 1.2) {
        ctx.setLineDash([]);
        ctx.strokeStyle = "rgba(249, 115, 22, 0.9)";
        ctx.lineWidth = 2.4;
      } else if (lk.p === "ble") {
        ctx.setLineDash([4, 4]);
        ctx.strokeStyle = "rgba(167, 139, 250, 0.36)";
        ctx.lineWidth = 1.1;
      } else {
        ctx.setLineDash([5, 4]);
        ctx.strokeStyle = "rgba(56, 189, 248, 0.4)";
        ctx.lineWidth = 1.2;
      }
      ctx.stroke();
    }
    ctx.setLineDash([]);

    // 5. Painter's Algorithm Depth-Sorted 3D Scene: Walls, Heatmap Cones, Devices, Receivers, Targets
    const drawables = [];

    for (const wp of wallPanels) {
      drawables.push({ depth: wp.depth, kind: "wall", item: wp });
    }
    const maxPeak = Math.max(2.5, ...data.peaks.map((pk) => pk[2] || 0));
    for (const pk of data.peaks) {
      const p = P(pk[0], pk[1], 0.02);
      drawables.push({ depth: p.depth, kind: "peak", item: pk, base: p });
    }
    for (const d of data.devices) {
      const top = P(d.x, d.y, d.z);
      drawables.push({ depth: top.depth, kind: "device", item: d, top });
    }
    for (const r of data.receivers) {
      const top = P(r.x, r.y, r.z);
      drawables.push({ depth: top.depth, kind: "receiver", item: r, top });
    }
    for (const t of data.targets) {
      const base = P(t.x, t.y, 0.03);
      drawables.push({ depth: base.depth, kind: "target", item: t, base });
    }
    drawables.sort((a, b) => a.depth - b.depth);

    for (const obj of drawables) {
      if (obj.kind === "wall") {
        const wp = obj.item;
        const fill = wp.col + "18";
        const stroke = wp.col + "66";
        if (!wp.doors.length) {
          this._drawWallQuad(ctx, P, wp.x0, wp.y0, wp.x1, wp.y1, 0, wallH, fill, stroke);
        } else {
          // Split wall horizontally around door cutouts + render top lintel & golden doorframe
          const len = Math.max(0.1, Math.hypot(wp.x1 - wp.x0, wp.y1 - wp.y0));
          const sortedDoors = [...wp.doors].sort((a, b) => a.pos - b.pos);
          let cursor = 0;
          for (const dr of sortedDoors) {
            const half = Math.min(0.45, (dr.width / len) * 0.5);
            const tA = Math.max(cursor, dr.pos - half);
            const tB = Math.min(1, dr.pos + half);
            if (tA > cursor + 0.01) {
              this._drawWallQuad(
                ctx,
                P,
                wp.x0 + cursor * (wp.x1 - wp.x0),
                wp.y0 + cursor * (wp.y1 - wp.y0),
                wp.x0 + tA * (wp.x1 - wp.x0),
                wp.y0 + tA * (wp.y1 - wp.y0),
                0,
                wallH,
                fill,
                stroke
              );
            }
            const dx0 = wp.x0 + tA * (wp.x1 - wp.x0);
            const dy0 = wp.y0 + tA * (wp.y1 - wp.y0);
            const dx1 = wp.x0 + tB * (wp.x1 - wp.x0);
            const dy1 = wp.y0 + tB * (wp.y1 - wp.y0);
            // Wall lintel above door (doorH .. wallH)
            this._drawWallQuad(ctx, P, dx0, dy0, dx1, dy1, doorH, wallH, fill, stroke);
            // Golden 3D doorframe
            const da0 = P(dx0, dy0, 0);
            const da1 = P(dx0, dy0, doorH);
            const db0 = P(dx1, dy1, 0);
            const db1 = P(dx1, dy1, doorH);
            ctx.strokeStyle = "#fde047";
            ctx.lineWidth = 2;
            ctx.beginPath();
            ctx.moveTo(da0.x, da0.y);
            ctx.lineTo(da1.x, da1.y);
            ctx.lineTo(db1.x, db1.y);
            ctx.lineTo(db0.x, db0.y);
            ctx.stroke();
            cursor = Math.max(cursor, tB);
          }
          if (cursor < 0.99) {
            this._drawWallQuad(
              ctx,
              P,
              wp.x0 + cursor * (wp.x1 - wp.x0),
              wp.y0 + cursor * (wp.y1 - wp.y0),
              wp.x1,
              wp.y1,
              0,
              wallH,
              fill,
              stroke
            );
          }
        }
      } else if (obj.kind === "peak") {
        const pk = obj.item;
        const t = Math.min(1, pk[2] / maxPeak);
        const coneH = 0.22 + 1.35 * t;
        const base = obj.base;
        const apex = P(pk[0], pk[1], coneH);
        const rad = Math.max(6, cam.baseScale * 0.36);
        ctx.beginPath();
        ctx.moveTo(base.x - rad, base.y);
        ctx.lineTo(apex.x, apex.y);
        ctx.lineTo(base.x + rad, base.y);
        ctx.closePath();
        ctx.fillStyle =
          t < 0.5
            ? `rgba(56, 189, 248, ${0.2 + 0.35 * t})`
            : `rgba(249, 115, 22, ${0.26 + 0.48 * t})`;
        ctx.fill();
      } else if (obj.kind === "device") {
        const d = obj.item;
        const base = P(d.x, d.y, 0);
        const top = obj.top;
        const col = d.protocol === "ble" ? "#c084fc" : "#38bdf8";
        const isSel = this._selectedItem && this._selectedItem.id === d.id;

        ctx.strokeStyle = "rgba(148, 163, 184, 0.45)";
        ctx.setLineDash([2, 3]);
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(base.x, base.y);
        ctx.lineTo(top.x, top.y);
        ctx.stroke();
        ctx.setLineDash([]);

        ctx.beginPath();
        ctx.arc(top.x, top.y, isSel ? 7 : 5, 0, Math.PI * 2);
        ctx.fillStyle = col;
        ctx.fill();
        ctx.strokeStyle = isSel ? "#fde047" : "#ffffff";
        ctx.lineWidth = isSel ? 2.5 : 1;
        ctx.stroke();

        if (this._mode === "edit" || isSel) {
          ctx.fillStyle = "#cbd5e1";
          ctx.font = "10px system-ui, sans-serif";
          ctx.fillText(
            `${d.name || d.id} (${Number(d.z ?? 1).toFixed(1)}m)`,
            top.x + 8,
            top.y + 3
          );
        }
      } else if (obj.kind === "receiver") {
        const r = obj.item;
        const base = P(r.x, r.y, 0);
        const top = obj.top;
        const isSel = this._selectedItem && this._selectedItem.id === r.id;

        ctx.strokeStyle = "rgba(250, 204, 21, 0.55)";
        ctx.setLineDash([2, 3]);
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(base.x, base.y);
        ctx.lineTo(top.x, top.y);
        ctx.stroke();
        ctx.setLineDash([]);

        const s = isSel ? 9 : 7;
        ctx.beginPath();
        ctx.moveTo(top.x, top.y - s);
        ctx.lineTo(top.x + s, top.y);
        ctx.lineTo(top.x, top.y + s);
        ctx.lineTo(top.x - s, top.y);
        ctx.closePath();
        ctx.fillStyle = "#facc15";
        ctx.fill();
        ctx.strokeStyle = isSel ? "#ffffff" : "#854d0e";
        ctx.lineWidth = 1.5;
        ctx.stroke();

        ctx.fillStyle = "#fef08a";
        ctx.font = "600 10px system-ui, sans-serif";
        ctx.fillText(r.name || r.id, top.x + 9, top.y + 3);
      } else if (obj.kind === "target") {
        const t = obj.item;
        const isHuman = t.type === "human";
        const col = isHuman ? "#10b981" : "#fb923c";
        const hiCol = isHuman ? "#6ee7b7" : "#fdba74";
        const base = obj.base;

        if (Array.isArray(t.trail) && t.trail.length >= 2) {
          for (let i = 0; i < t.trail.length - 1; i++) {
            const p0 = P(t.trail[i][0], t.trail[i][1], 0.04);
            const p1 = P(t.trail[i + 1][0], t.trail[i + 1][1], 0.04);
            const alpha = 0.15 + 0.75 * ((i + 1) / (t.trail.length - 1));
            ctx.strokeStyle = isHuman
              ? `rgba(16,185,129,${alpha})`
              : `rgba(251,146,60,${alpha})`;
            ctx.lineWidth = 2.4;
            ctx.beginPath();
            ctx.moveTo(p0.x, p0.y);
            ctx.lineTo(p1.x, p1.y);
            ctx.stroke();
          }
        }

        const hr = (12 + 5 * pulse) * Math.max(0.7, this._zoom * 0.85);
        ctx.beginPath();
        ctx.ellipse(base.x, base.y, hr, hr * 0.52, 0, 0, Math.PI * 2);
        ctx.fillStyle = isHuman ? "rgba(16,185,129,0.22)" : "rgba(251,146,60,0.24)";
        ctx.fill();
        ctx.strokeStyle = col;
        ctx.lineWidth = 1.2;
        ctx.stroke();

        let labelY = base.y - 26;
        if (isHuman) {
          const topTorso = P(t.x, t.y, 1.38);
          const head = P(t.x, t.y, 1.66);
          const bw = Math.max(5, cam.baseScale * 0.18);

          ctx.beginPath();
          ctx.moveTo(base.x - bw, base.y);
          ctx.lineTo(base.x + bw, base.y);
          ctx.lineTo(topTorso.x + bw * 1.15, topTorso.y);
          ctx.lineTo(topTorso.x - bw * 1.15, topTorso.y);
          ctx.closePath();
          ctx.fillStyle = col;
          ctx.fill();
          ctx.strokeStyle = hiCol;
          ctx.lineWidth = 1.2;
          ctx.stroke();

          const headR = Math.max(5, cam.baseScale * 0.16);
          ctx.beginPath();
          ctx.arc(head.x, head.y, headR, 0, Math.PI * 2);
          ctx.fillStyle = col;
          ctx.fill();
          ctx.strokeStyle = "#ffffff";
          ctx.lineWidth = 1.5;
          ctx.stroke();
          labelY = head.y - headR - 8;
        } else {
          const dome = P(t.x, t.y, 0.24);
          const drx = Math.max(7, cam.baseScale * 0.24);
          const dry = Math.max(5, cam.baseScale * 0.17);
          ctx.beginPath();
          ctx.ellipse(dome.x, dome.y, drx, dry, 0, 0, Math.PI * 2);
          ctx.fillStyle = col;
          ctx.fill();
          ctx.strokeStyle = "#ffffff";
          ctx.lineWidth = 1.5;
          ctx.stroke();
          labelY = dome.y - dry - 8;
        }

        const badge = `${isHuman ? "M" : "T"}${t.id} ${Math.round((t.confidence || 0) * 100)}%`;
        ctx.font = "600 11px system-ui, sans-serif";
        const tw = ctx.measureText(badge).width + 10;
        ctx.fillStyle = "rgba(15, 23, 42, 0.9)";
        ctx.fillRect(base.x - tw / 2, labelY - 13, tw, 17);
        ctx.strokeStyle = col;
        ctx.lineWidth = 1;
        ctx.strokeRect(base.x - tw / 2, labelY - 13, tw, 17);
        ctx.fillStyle = "#f8fafc";
        ctx.textAlign = "center";
        ctx.fillText(badge, base.x, labelY - 1);
        ctx.textAlign = "start";
      }
    }
  }
}

/**
 * Full-Viewport Home Assistant Sidebar Panel (<rf-presence-3d-panel>)
 * Mounted at /rf-presence-3d via panel_custom.async_register_panel.
 */
class RFPresence3DPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._narrow = false;
    this._panel = null;
    this._card = null;
  }

  set hass(hass) {
    this._hass = hass;
    this._ensureDom();
    if (this._card) {
      this._card.hass = hass;
    }
  }

  set narrow(val) {
    this._narrow = Boolean(val);
    this._updateMenuBtn();
  }

  set panel(val) {
    this._panel = val;
  }

  connectedCallback() {
    this._ensureDom();
    this._updateMenuBtn();
  }

  _updateMenuBtn() {
    const btn = this.shadowRoot && this.shadowRoot.getElementById("menu-btn");
    if (btn) {
      btn.style.display = "inline-flex";
    }
  }

  _ensureDom() {
    if (this._ready) return;
    this._ready = true;
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: flex;
          flex-direction: column;
          width: 100%;
          height: 100vh;
          height: 100dvh;
          background: #090f1d;
          overflow: hidden;
        }
        .topbar {
          display: flex;
          align-items: center;
          gap: 10px;
          height: 48px;
          padding: 0 12px;
          background: #0f172a;
          color: #f8fafc;
          border-bottom: 1px solid rgba(100, 116, 139, 0.3);
          flex: 0 0 auto;
          font-family: var(--paper-font-body1_-_font-family, system-ui, -apple-system, sans-serif);
        }
        .menu-btn {
          background: #1e293b;
          color: #e2e8f0;
          border: 1px solid #334155;
          border-radius: 6px;
          width: 34px;
          height: 34px;
          font-size: 18px;
          cursor: pointer;
          display: inline-flex;
          align-items: center;
          justify-content: center;
        }
        .menu-btn:hover { background: #334155; }
        .topbar-title {
          font-size: 16px;
          font-weight: 600;
          letter-spacing: 0.2px;
        }
        .body {
          flex: 1 1 auto;
          min-height: 0;
          display: flex;
        }
        rf-presence-3d-card {
          width: 100%;
          height: 100%;
        }
      </style>
      <div class="topbar">
        <button id="menu-btn" class="menu-btn" aria-label="Menü">☰</button>
        <div class="topbar-title">RF Presence Sense — 3D Live-Map</div>
      </div>
      <div class="body" id="body"></div>
    `;

    this.shadowRoot.getElementById("menu-btn").addEventListener("click", () => {
      this.dispatchEvent(
        new CustomEvent("hass-toggle-menu", { bubbles: true, composed: true })
      );
    });

    const card = document.createElement("rf-presence-3d-card");
    card.setConfig({
      title: "3D Wohnung & Live-Ortung",
      full_panel: true,
    });
    if (this._hass) {
      card.hass = this._hass;
    }
    this._card = card;
    this.shadowRoot.getElementById("body").appendChild(card);
  }
}

if (!customElements.get("rf-presence-3d-card")) {
  customElements.define("rf-presence-3d-card", RFPresence3DCard);
}
if (!customElements.get("rf-presence-3d-panel")) {
  customElements.define("rf-presence-3d-panel", RFPresence3DPanel);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "rf-presence-3d-card")) {
  window.customCards.push({
    type: "rf-presence-3d-card",
    name: "RF Presence Sense 3D Map",
    description:
      "Interactive 3D Wi-Fi & BLE radio tomographic live map with Drag & Drop editor and Walk-Calibration wizard.",
  });
}
