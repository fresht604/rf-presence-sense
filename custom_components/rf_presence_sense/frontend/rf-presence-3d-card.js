/**
 * RF Presence Sense — Interactive 3D Custom Lovelace Card (v2.0.0)
 * Pure Vanilla JS + HTML5 3D Canvas (zero external dependencies).
 *
 * Features:
 *   - Interactive 3D orbit (mouse/touch drag), zoom (wheel), 3D / Top-Down 2D toggle
 *   - Optional floorplan_image projected onto the 3D floor plane
 *   - Live 3D rooms (rect & polygon), 3D RF links at real z-height, 3D heatmap cones,
 *     animated 3D human & animal avatars with fading movement trails
 *   - Built-in Edit Mode: Drag & Drop devices, receivers & room corners directly on
 *     the 3D map, tune Z height, and save via rf_presence_sense services
 *   - Quick actions: Calibrate Empty Room & Reset Baseline
 */

class RFPresence3DCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._hass = null;

    // 3D camera spherical angles (radians)
    this._yaw = (38 * Math.PI) / 180;
    this._pitch = (34 * Math.PI) / 180;
    this._zoom = 1.0;

    // Interaction state
    this._editMode = false;
    this._draggingCamera = false;
    this._draggingItem = null; // { kind: 'device'|'receiver'|'room_corner', id, corner? }
    this._selectedItem = null;
    this._lastPtr = { x: 0, y: 0 };

    // Local draft overrides while dragging in Edit Mode
    this._draftPositions = {}; // id -> { x, y, z, name }
    this._draftRooms = {}; // room_id -> { id, name, x_min, y_min, x_max, y_max }

    // Optional floorplan image & state caching
    this._floorImg = null;
    this._floorImgUrl = null;
    this._rafId = null;
    this._statusMsg = "";
    this._lastStateObj = null;
    this._resolvedEntityId = null;
  }

  static getStubConfig() {
    return {
      title: "RF Presence Sense 3D",
      entity: "sensor.rf_presence_sense_targets_detail",
      height: 430,
    };
  }

  setConfig(config) {
    if (!config) {
      throw new Error("Invalid configuration for rf-presence-3d-card");
    }
    const safeHeight = Math.max(
      200,
      Math.min(1200, Math.round(Number(config.height) || 430))
    );
    this._config = {
      title: String(config.title || "RF Presence Sense 3D"),
      entity: String(config.entity || ""),
      floorplan_image: String(config.floorplan_image || ""),
      height: safeHeight,
    };
    this._resolvedEntityId = null;
    this._lastStateObj = null;
    this._loadFloorplanImage(this._config.floorplan_image);
    this._ensureDom();
    const vp = this.shadowRoot && this.shadowRoot.querySelector(".viewport");
    if (vp) vp.style.height = `${safeHeight}px`;
    this._updateHud();
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
    this._renderScene();
    this._syncPulseLoop();
  }

  getCardSize() {
    return Math.max(5, Math.round((this._config.height || 430) / 70));
  }

  connectedCallback() {
    this._ensureDom();
    if (!this._windowBound) {
      this._boundPointerMove = (ev) => this._onPointerMove(ev);
      this._boundPointerUp = () => this._onPointerUp();
      this._boundVisibility = () => this._syncPulseLoop();
      window.addEventListener("pointermove", this._boundPointerMove);
      window.addEventListener("pointerup", this._boundPointerUp);
      document.addEventListener("visibilitychange", this._boundVisibility);
      this._windowBound = true;
    }
    this._syncPulseLoop();
  }

  disconnectedCallback() {
    this._stopPulseLoop();
    if (this._windowBound) {
      window.removeEventListener("pointermove", this._boundPointerMove);
      window.removeEventListener("pointerup", this._boundPointerUp);
      document.removeEventListener("visibilitychange", this._boundVisibility);
      this._windowBound = false;
    }
    if (this._toastTimer) {
      clearTimeout(this._toastTimer);
      this._toastTimer = null;
    }
  }

  _hasActiveTargets() {
    const st = this._getStateObj();
    return Boolean(
      st &&
        st.attributes &&
        Array.isArray(st.attributes.targets) &&
        st.attributes.targets.length > 0
    );
  }

  _stopPulseLoop() {
    if (this._rafId) {
      cancelAnimationFrame(this._rafId);
      this._rafId = null;
    }
  }

  _syncPulseLoop() {
    if (!this.isConnected || document.hidden || !this._hasActiveTargets()) {
      this._stopPulseLoop();
      return;
    }
    if (this._rafId) return;
    let lastDraw = 0;
    const tick = (ts) => {
      if (!this.isConnected || document.hidden || !this._hasActiveTargets()) {
        this._rafId = null;
        return;
      }
      // Throttle ambient pulse animation (~10fps) only while targets are present
      if (ts - lastDraw >= 100) {
        this._renderScene();
        lastDraw = ts;
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
    // Auto-discover RF Presence Sense detail sensor once and cache entity_id
    for (const [eid, st] of Object.entries(this._hass.states)) {
      if (
        eid.startsWith("sensor.") &&
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

  _getSceneData() {
    const st = this._getStateObj();
    const attrs = (st && st.attributes) || {};
    const geo = attrs.geometry || {};
    const fw = Math.max(1, Number(geo.floor_width_m) || 10);
    const fh = Math.max(1, Number(geo.floor_height_m) || 8);

    const devices = (attrs.devices || []).map((raw) => {
      // Support both compact tuple [id, name, "w"|"b", x, y, z] and legacy object
      const d = Array.isArray(raw)
        ? {
            id: raw[0],
            name: raw[1] || raw[0],
            protocol: raw[2] === "b" || raw[2] === "ble" ? "ble" : "wifi",
            x: Number(raw[3]) || 0,
            y: Number(raw[4]) || 0,
            z: Number(raw[5]) || 1,
          }
        : {
            ...raw,
            name: raw.name || raw.id,
            protocol: raw.protocol || (raw.p === "b" ? "ble" : "wifi"),
          };
      const draft = this._draftPositions[d.id];
      return draft ? { ...d, x: draft.x, y: draft.y, z: draft.z } : d;
    });
    const receivers = (attrs.receivers || []).map((r) => {
      const draft = this._draftPositions[r.id];
      const name = r.name || r.id;
      return draft
        ? { ...r, name, x: draft.x, y: draft.y, z: draft.z }
        : { ...r, name };
    });
    const rooms = (attrs.rooms_geometry || []).map((rm) => {
      const draft = this._draftRooms[rm.id];
      return draft ? { ...rm, ...draft } : { ...rm };
    });

    // Build lookup for draft-aware 3D links
    const posLookup = {};
    for (const d of devices) posLookup[d.id] = [d.x, d.y, d.z];
    for (const r of receivers) posLookup[r.id] = [r.x, r.y, r.z];

    const links = (attrs.active_links_3d || []).map((lk) => {
      // Compact indexed format: [devIdx, recvIdx, "w"|"b", pert?]
      if (Array.isArray(lk)) {
        const d = devices[lk[0]];
        const r = receivers[lk[1]];
        const p1 = d ? [d.x, d.y, d.z] : [0, 0, 1];
        const p2 = r ? [r.x, r.y, r.z] : [fw / 2, fh / 2, 1];
        const proto = lk[2] === "b" || lk[2] === "ble" ? "ble" : "wifi";
        const pert = Number(lk[3]) || 0;
        return { p: proto, p1, p2, pert };
      }
      // Legacy object format: { id, p, p1, p2, pert }
      const parts = String(lk.id || "").split("->");
      const p1 = (parts[0] && posLookup[parts[0]]) || lk.p1 || [0, 0, 1];
      const p2 = (parts[1] && posLookup[parts[1]]) || lk.p2 || [fw / 2, fh / 2, 1];
      const proto = lk.p === "b" || lk.p === "ble" ? "ble" : "wifi";
      return { ...lk, p: proto, p1, p2 };
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
    };
  }

  _ensureDom() {
    if (this._domReady) return;
    this._domReady = true;

    const h = this._config.height || 430;
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        ha-card {
          background: var(--ha-card-background, #0f172a);
          color: var(--primary-text-color, #e2e8f0);
          border-radius: var(--ha-card-border-radius, 12px);
          overflow: hidden;
          font-family: var(--paper-font-body1_-_font-family, system-ui, -apple-system, sans-serif);
        }
        .header {
          display: flex;
          flex-wrap: wrap;
          align-items: center;
          justify-content: space-between;
          gap: 8px;
          padding: 10px 14px;
          background: rgba(15, 23, 42, 0.92);
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
        .pill.cal { border-color: #f59e0b; background: rgba(217,119,6,0.28); color: #fde047; }
        .toolbar {
          display: flex;
          flex-wrap: wrap;
          gap: 6px;
          padding: 8px 12px;
          background: rgba(15, 23, 42, 0.85);
          border-bottom: 1px solid rgba(100, 116, 139, 0.25);
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
        button.warn {
          border-color: #f59e0b;
          color: #fde68a;
        }
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
          background: #0b1120;
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
        canvas.editing { cursor: crosshair; }
        .hint {
          position: absolute;
          bottom: 8px;
          left: 10px;
          font-size: 11px;
          color: rgba(203, 213, 225, 0.72);
          background: rgba(15, 23, 42, 0.72);
          padding: 3px 8px;
          border-radius: 6px;
          pointer-events: none;
        }
        .toast {
          position: absolute;
          top: 10px;
          right: 10px;
          font-size: 12px;
          color: #f8fafc;
          background: rgba(5, 150, 105, 0.92);
          padding: 5px 10px;
          border-radius: 6px;
          display: none;
        }
        .editor-panel {
          display: none;
          padding: 10px 14px;
          background: #0f172a;
          border-top: 1px solid #334155;
          font-size: 12px;
          color: #cbd5e1;
        }
        .editor-panel.open { display: block; }
        .editor-row {
          display: flex;
          flex-wrap: wrap;
          align-items: center;
          gap: 8px;
          margin-bottom: 8px;
        }
        .editor-row label {
          display: inline-flex;
          align-items: center;
          gap: 4px;
        }
        input[type="text"], input[type="number"] {
          background: #1e293b;
          color: #f8fafc;
          border: 1px solid #475569;
          border-radius: 4px;
          padding: 3px 6px;
          font-size: 12px;
          width: 72px;
        }
        input[type="text"] { width: 115px; }
        input[type="range"] { width: 110px; accent-color: #38bdf8; }
      </style>
      <ha-card>
        <div class="header">
          <div class="title" id="card-title"></div>
          <div class="pills" id="hud-pills"></div>
        </div>
        <div class="toolbar">
          <button id="btn-3d" class="active">3D</button>
          <button id="btn-2d">2D Top</button>
          <button id="btn-reset-view">Reset View</button>
          <button id="btn-edit">Edit Mode (Drag & Drop)</button>
          <button id="btn-calibrate" class="warn">Calibrate</button>
          <button id="btn-reset-base">Reset Baseline</button>
        </div>
        <div class="viewport">
          <canvas id="canvas"></canvas>
          <div class="hint" id="hint-text">Drag: Orbit 3D | Scroll: Zoom</div>
          <div class="toast" id="toast"></div>
        </div>
        <div class="editor-panel" id="editor-panel">
          <div class="editor-row" id="pos-editor">
            <strong id="sel-label">Select a device/receiver on the map to drag</strong>
            <label>X(m)<input type="number" id="in-x" step="0.1" min="0" max="50"></label>
            <label>Y(m)<input type="number" id="in-y" step="0.1" min="0" max="50"></label>
            <label>Z(m)<input type="range" id="in-z" min="0" max="3.0" step="0.05"><span id="val-z">1.0m</span></label>
            <button id="btn-save-pos" class="success">Save Position</button>
          </div>
          <div class="editor-row">
            <strong>Room:</strong>
            <input type="text" id="rm-id" placeholder="room_id">
            <input type="text" id="rm-name" placeholder="Name">
            <label>X1<input type="number" id="rm-x1" step="0.1" value="0"></label>
            <label>Y1<input type="number" id="rm-y1" step="0.1" value="0"></label>
            <label>X2<input type="number" id="rm-x2" step="0.1" value="5"></label>
            <label>Y2<input type="number" id="rm-y2" step="0.1" value="4"></label>
            <button id="btn-save-room" class="success">Save Room</button>
            <button id="btn-del-room" class="danger">Delete Room</button>
          </div>
        </div>
      </ha-card>
    `;

    this._canvas = this.shadowRoot.getElementById("canvas");
    this._ctx = this._canvas.getContext("2d");
    this._bindEvents();
    this._updateHud();
  }

  _showToast(msg) {
    const el = this.shadowRoot && this.shadowRoot.getElementById("toast");
    if (!el) return;
    el.textContent = msg;
    el.style.display = "block";
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => {
      el.style.display = "none";
    }, 2400);
  }

  _updateHud() {
    if (!this.shadowRoot) return;
    const titleEl = this.shadowRoot.getElementById("card-title");
    if (titleEl) titleEl.textContent = this._config.title || "RF Presence Sense 3D";

    const pills = this.shadowRoot.getElementById("hud-pills");
    if (!pills) return;
    const data = this._getSceneData();
    const humans = data.targets.filter((t) => t.type === "human").length;
    const animals = data.targets.filter((t) => t.type !== "human").length;
    const activePert = data.links.filter((l) => (l.pert || 0) > 1.2).length;

    const makePill = (cls, text) => {
      const span = document.createElement("span");
      span.className = cls;
      span.textContent = text;
      return span;
    };
    pills.replaceChildren(
      makePill("pill human", `Humans: ${humans}`),
      makePill("pill animal", `Animals: ${animals}`),
      makePill("pill", `Links: ${data.links.length} (${activePert} active)`),
      ...(data.calibrating ? [makePill("pill cal", "Calibrating...")] : [])
    );
  }

  _bindEvents() {
    const $ = (id) => this.shadowRoot.getElementById(id);

    $("btn-3d").addEventListener("click", () => {
      this._yaw = (38 * Math.PI) / 180;
      this._pitch = (34 * Math.PI) / 180;
      $("btn-3d").classList.add("active");
      $("btn-2d").classList.remove("active");
      this._renderScene();
    });

    $("btn-2d").addEventListener("click", () => {
      this._yaw = 0;
      this._pitch = (89 * Math.PI) / 180;
      $("btn-2d").classList.add("active");
      $("btn-3d").classList.remove("active");
      this._renderScene();
    });

    $("btn-reset-view").addEventListener("click", () => {
      this._yaw = (38 * Math.PI) / 180;
      this._pitch = (34 * Math.PI) / 180;
      this._zoom = 1.0;
      $("btn-3d").classList.add("active");
      $("btn-2d").classList.remove("active");
      this._renderScene();
    });

    $("btn-edit").addEventListener("click", () => {
      this._editMode = !this._editMode;
      $("btn-edit").classList.toggle("active", this._editMode);
      $("editor-panel").classList.toggle("open", this._editMode);
      this._canvas.classList.toggle("editing", this._editMode);
      $("hint-text").textContent = this._editMode
        ? "Edit Mode: Click & Drag devices/receivers/room corners | Right-drag or empty space: Orbit"
        : "Drag: Orbit 3D | Scroll: Zoom";
      if (!this._editMode) {
        this._draftPositions = {};
        this._draftRooms = {};
        this._selectedItem = null;
      }
      this._renderScene();
    });

    $("btn-calibrate").addEventListener("click", () => {
      if (!this._hass) return;
      this._hass.callService("rf_presence_sense", "calibrate_empty_room", {});
      this._showToast("Calibration started...");
    });

    $("btn-reset-base").addEventListener("click", () => {
      if (!this._hass) return;
      this._hass.callService("rf_presence_sense", "reset_baseline", {});
      this._showToast("Baseline reset!");
    });

    // Live X/Y/Z input sync for selected device/receiver
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
        this._showToast("Select a device or receiver first");
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
      this._showToast(`Saved ${this._selectedItem.name} (${x.toFixed(1)}, ${y.toFixed(1)}, z=${z.toFixed(2)}m)`);
    });

    $("btn-save-room").addEventListener("click", () => {
      if (!this._hass) return;
      const room_id = ($("rm-id").value || "").trim();
      const name = ($("rm-name").value || "").trim() || room_id;
      if (!room_id) {
        this._showToast("Enter a room_id first");
        return;
      }
      this._hass.callService("rf_presence_sense", "define_room", {
        room_id,
        name,
        x_min: Number($("rm-x1").value) || 0,
        y_min: Number($("rm-y1").value) || 0,
        x_max: Number($("rm-x2").value) || 5,
        y_max: Number($("rm-y2").value) || 4,
      });
      delete this._draftRooms[room_id];
      this._showToast(`Saved room "${name}"`);
    });

    $("btn-del-room").addEventListener("click", () => {
      if (!this._hass) return;
      const room_id = ($("rm-id").value || "").trim();
      if (!room_id) {
        this._showToast("Enter room_id to delete");
        return;
      }
      this._hass.callService("rf_presence_sense", "delete_room", { room_id });
      delete this._draftRooms[room_id];
      this._showToast(`Deleted room "${room_id}"`);
    });

    // Pointer interactions on 3D Canvas (window move/up bound in connectedCallback)
    const canvas = this._canvas;
    canvas.addEventListener("pointerdown", (ev) => this._onPointerDown(ev));
    canvas.addEventListener(
      "wheel",
      (ev) => {
        ev.preventDefault();
        const factor = ev.deltaY > 0 ? 0.92 : 1.08;
        this._zoom = Math.max(0.55, Math.min(2.6, this._zoom * factor));
        this._renderScene();
      },
      { passive: false }
    );
  }

  _buildCamera(w, h, fw, fh) {
    const cx = fw * 0.5;
    const cy = fh * 0.5;
    const cz = 0.7;
    const cosY = Math.cos(this._yaw);
    const sinY = Math.sin(this._yaw);
    const cosP = Math.cos(this._pitch);
    const sinP = Math.sin(this._pitch);
    const diag = Math.hypot(fw, fh, 2.5);
    const baseScale = (Math.min(w, h) * 0.72 * this._zoom) / Math.max(diag, 1);

    const project = (x, y, z) => {
      const dx = x - cx;
      const dy = y - cy;
      const dz = z - cz;
      // Rotate around Z by yaw
      const rx = dx * cosY - dy * sinY;
      const ry = dx * sinY + dy * cosY;
      // Tilt by pitch (screen Y goes down)
      const sx = w * 0.5 + rx * baseScale;
      const sy = h * 0.54 + (ry * sinP - dz * cosP) * baseScale;
      const depth = ry * cosP + dz * sinP;
      return { x: sx, y: sy, depth };
    };

    // Inverse projection from canvas pixel (px, py) onto horizontal plane z = planeZ
    const unprojectToPlane = (px, py, planeZ) => {
      const dz = planeZ - cz;
      const rx = (px - w * 0.5) / baseScale;
      const ry =
        Math.abs(sinP) > 0.08
          ? ((py - h * 0.54) / baseScale + dz * cosP) / sinP
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
      const corners = [
        { key: "min", x: rm.x_min, y: rm.y_min },
        { key: "max", x: rm.x_max, y: rm.y_max },
      ];
      for (const c of corners) {
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
    } else {
      $("sel-label").textContent = `${sel.kind === "receiver" ? "[RX] " : ""}${sel.name}`;
      $("in-x").value = Number(sel.x).toFixed(2);
      $("in-y").value = Number(sel.y).toFixed(2);
      $("in-z").value = Number(sel.z).toFixed(2);
      $("val-z").textContent = `${Number(sel.z).toFixed(2)}m`;
    }
  }

  _onPointerDown(ev) {
    const rect = this._canvas.getBoundingClientRect();
    const px = ev.clientX - rect.left;
    const py = ev.clientY - rect.top;
    this._lastPtr = { x: ev.clientX, y: ev.clientY };

    if (this._editMode && ev.button === 0) {
      const data = this._getSceneData();
      const cam = this._buildCamera(rect.width, rect.height, data.fw, data.fh);
      const hit = this._hitTestEditHandle(px, py, data, cam);
      if (hit) {
        this._selectedItem = hit;
        this._draggingItem = hit;
        this._populateEditorForSelection(hit);
        this._canvas.classList.add("dragging");
        this._renderScene();
        return;
      }
    }

    this._draggingCamera = true;
    this._canvas.classList.add("dragging");
  }

  _onPointerMove(ev) {
    if (!this._draggingCamera && !this._draggingItem) return;
    const rect = this._canvas.getBoundingClientRect();

    if (this._draggingItem) {
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
        const z = this._draggingItem.z;
        const nextPos = {
          x: Math.round(pt.x * 20) / 20,
          y: Math.round(pt.y * 20) / 20,
          z,
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
    this._yaw += dx * 0.008;
    this._pitch = Math.max(0.18, Math.min(1.55, this._pitch + dy * 0.007));
    this._renderScene();
  }

  _onPointerUp() {
    this._draggingCamera = false;
    this._draggingItem = null;
    if (this._canvas) this._canvas.classList.remove("dragging");
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

    // Background radial glow
    const grad = ctx.createRadialGradient(w * 0.5, h * 0.45, 20, w * 0.5, h * 0.5, Math.max(w, h) * 0.7);
    grad.addColorStop(0, "#132038");
    grad.addColorStop(1, "#090f1d");
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, w, h);

    const data = this._getSceneData();
    const { fw, fh } = data;
    const cam = this._buildCamera(w, h, fw, fh);
    const P = cam.project;
    const pulse = 0.5 + 0.5 * Math.sin(performance.now() * 0.0045);

    // 1. Floor slab
    const c00 = P(0, 0, 0);
    const cw0 = P(fw, 0, 0);
    const cwh = P(fw, fh, 0);
    const c0h = P(0, fh, 0);

    ctx.beginPath();
    ctx.moveTo(c00.x, c00.y);
    ctx.lineTo(cw0.x, cw0.y);
    ctx.lineTo(cwh.x, cwh.y);
    ctx.lineTo(c0h.x, c0h.y);
    ctx.closePath();
    ctx.fillStyle = "#1e293b";
    ctx.fill();
    ctx.strokeStyle = "#475569";
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // Optional floorplan_image affine mapped onto floor quad (c00 -> cw0, c00 -> c0h)
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
      ctx.globalAlpha = 0.52;
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

    // 2. 1-meter 3D floor grid
    ctx.strokeStyle = "rgba(100, 116, 139, 0.25)";
    ctx.lineWidth = 1;
    const step = Math.max(fw, fh) > 15 ? 2 : 1;
    for (let x = step; x < fw; x += step) {
      const a = P(x, 0, 0);
      const b = P(x, fh, 0);
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    }
    for (let y = step; y < fh; y += step) {
      const a = P(0, y, 0);
      const b = P(fw, y, 0);
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    }

    // Z reference bar
    const z0 = P(0, 0, 0);
    const z2 = P(0, 0, 2.2);
    ctx.strokeStyle = "rgba(148, 163, 184, 0.55)";
    ctx.beginPath();
    ctx.moveTo(z0.x, z0.y);
    ctx.lineTo(z2.x, z2.y);
    ctx.stroke();
    ctx.fillStyle = "#94a3b8";
    ctx.font = "10px system-ui, sans-serif";
    ctx.fillText("2.2m", z2.x + 4, z2.y + 3);

    // 3. 3D Rooms (rectangular or polygon)
    const roomColors = ["#38bdf8", "#818cf8", "#34d399", "#fbbf24", "#f472b6"];
    data.rooms.forEach((rm, idx) => {
      const col = roomColors[idx % roomColors.length];
      const verts =
        rm.polygon && rm.polygon.length >= 3
          ? rm.polygon
          : [
              [rm.x_min, rm.y_min],
              [rm.x_max, rm.y_min],
              [rm.x_max, rm.y_max],
              [rm.x_min, rm.y_max],
            ];
      const floorPts = verts.map(([vx, vy]) => P(vx, vy, 0.01));
      const ceilPts = verts.map(([vx, vy]) => P(vx, vy, 2.2));

      // Floor tint
      ctx.beginPath();
      floorPts.forEach((p, i) => (i === 0 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y)));
      ctx.closePath();
      ctx.fillStyle = col + "18";
      ctx.fill();
      ctx.strokeStyle = col + "aa";
      ctx.lineWidth = 1.4;
      ctx.stroke();

      // 3D walls & ceiling outline
      ctx.strokeStyle = col + "48";
      ctx.lineWidth = 1;
      for (let i = 0; i < verts.length; i++) {
        const j = (i + 1) % verts.length;
        ctx.beginPath();
        ctx.moveTo(floorPts[i].x, floorPts[i].y);
        ctx.lineTo(ceilPts[i].x, ceilPts[i].y);
        ctx.lineTo(ceilPts[j].x, ceilPts[j].y);
        ctx.stroke();
      }

      // Room label
      const cx = verts.reduce((s, v) => s + v[0], 0) / verts.length;
      const cy = verts.reduce((s, v) => s + v[1], 0) / verts.length;
      const lp = P(cx, cy, 0.05);
      ctx.font = "600 11px system-ui, sans-serif";
      ctx.fillStyle = "#e2e8f0";
      ctx.textAlign = "center";
      ctx.fillText(rm.name || rm.id, lp.x, lp.y);

      // Edit handles on corners
      if (this._editMode && !(rm.polygon && rm.polygon.length >= 3)) {
        for (const [cxm, cym] of [
          [rm.x_min, rm.y_min],
          [rm.x_max, rm.y_max],
        ]) {
          const hp = P(cxm, cym, 0.05);
          ctx.fillStyle = col;
          ctx.fillRect(hp.x - 5, hp.y - 5, 10, 10);
          ctx.strokeStyle = "#ffffff";
          ctx.strokeRect(hp.x - 5, hp.y - 5, 10, 10);
        }
      }
    });
    ctx.textAlign = "start";

    // 4. 3D RTI Heatmap Cones
    const maxPeak = Math.max(2.5, ...data.peaks.map((pk) => pk[2] || 0));
    const sortedPeaks = data.peaks
      .map((pk) => ({ x: pk[0], y: pk[1], v: pk[2], p: P(pk[0], pk[1], 0.02) }))
      .sort((a, b) => a.p.depth - b.p.depth);

    for (const pk of sortedPeaks) {
      const t = Math.min(1, pk.v / maxPeak);
      const coneH = 0.2 + 1.3 * t;
      const base = pk.p;
      const apex = P(pk.x, pk.y, coneH);
      const rad = Math.max(6, cam.baseScale * 0.38);
      const color =
        t < 0.5
          ? `rgba(56, 189, 248, ${0.18 + 0.35 * t})`
          : `rgba(249, 115, 22, ${0.25 + 0.48 * t})`;

      ctx.beginPath();
      ctx.moveTo(base.x - rad, base.y);
      ctx.lineTo(apex.x, apex.y);
      ctx.lineTo(base.x + rad, base.y);
      ctx.closePath();
      ctx.fillStyle = color;
      ctx.fill();
    }

    // 5. True 3D RF Links
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
        ctx.strokeStyle = "rgba(167, 139, 250, 0.38)";
        ctx.lineWidth = 1.1;
      } else {
        ctx.setLineDash([5, 4]);
        ctx.strokeStyle = "rgba(56, 189, 248, 0.42)";
        ctx.lineWidth = 1.2;
      }
      ctx.stroke();
    }
    ctx.setLineDash([]);

    // 6. Depth-sorted 3D Objects: Devices, Receivers & Human/Animal Avatars
    const drawables = [];

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
      if (obj.kind === "device") {
        const d = obj.item;
        const base = P(d.x, d.y, 0);
        const top = obj.top;
        const col = d.protocol === "ble" ? "#c084fc" : "#38bdf8";
        const isSel = this._selectedItem && this._selectedItem.id === d.id;

        ctx.strokeStyle = "rgba(148, 163, 184, 0.45)";
        ctx.setLineDash([2, 3]);
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

        if (this._editMode || isSel) {
          ctx.fillStyle = "#cbd5e1";
          ctx.font = "10px system-ui, sans-serif";
          ctx.fillText(
            `${d.name || d.id} (${Number(d.z || 0).toFixed(1)}m)`,
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

        // Movement trail on floor
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

        // Pulsing ground halo
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
        ctx.fillStyle = "rgba(15, 23, 42, 0.88)";
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

if (!customElements.get("rf-presence-3d-card")) {
  customElements.define("rf-presence-3d-card", RFPresence3DCard);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "rf-presence-3d-card")) {
  window.customCards.push({
    type: "rf-presence-3d-card",
    name: "RF Presence Sense 3D Map",
    description:
      "Interactive 3D Wi-Fi & BLE radio tomographic live map with Drag & Drop editor.",
  });
}
