/* MASS/ROC 콘솔 공용 스크립트
   - 4 Hz로 /api/state를 폴링해 화면을 갱신한다.
   - 해도는 브라우저가 SVG로 직접 그린다. 서버는 위경도 숫자만 준다.
   - 좌표 변환은 common/observe/report.py의 _Projection과 같은 방식이다
     (경도는 cos(위도)로 보정해 항적이 찌그러지지 않게 한다). */

(() => {
  "use strict";

  const PAGE = document.body.dataset.console; // "roc" | "mass"
  const POLL_MS = 250;
  const SVG_NS = "http://www.w3.org/2000/svg";

  const $ = (sel) => document.querySelector(sel);

  // ------------------------------------------------------------ 표시 도우미

  const pad2 = (n) => String(n).padStart(2, "0");

  // 시뮬레이션 시각 → "T+mm:ss.s"
  function fmtTime(t) {
    if (t == null) return "—";
    const m = Math.floor(t / 60);
    const s = (t - m * 60).toFixed(1);
    return `T+${pad2(m)}:${s.padStart(4, "0")}`;
  }

  // 도 → 도·분(DMM). 해도·GNSS 계기 표기.
  function fmtDMM(value, isLat) {
    if (value == null || Number.isNaN(value)) return "—";
    const hemi = isLat ? (value >= 0 ? "N" : "S") : (value >= 0 ? "E" : "W");
    const abs = Math.abs(value);
    const deg = Math.floor(abs);
    const min = (abs - deg) * 60;
    const degText = isLat ? pad2(deg) : String(deg).padStart(3, "0");
    return `${degText}° ${min.toFixed(3).padStart(6, "0")}′ ${hemi}`;
  }

  const fmtHeading = (h) => (h == null ? "—" : `${h.toFixed(1).padStart(5, "0")}°`);
  const fmtKnots = (v) => (v == null ? "—" : v.toFixed(1));

  const MODE_TEXT = {
    1: ["RC", "원격제어 — 제어권 ROC"],
    2: ["MON", "감독 자율 — 제어권 본선"],
    3: ["AUT", "독립 자율 — 세션 없음"],
    0: ["?", "알 수 없음"],
  };

  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "html") node.innerHTML = value;
      else node.setAttribute(key, value);
    }
    for (const child of children) node.appendChild(child);
    return node;
  }

  function svgEl(tag, attrs = {}) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    return node;
  }

  async function api(path, body) {
    const response = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  // ------------------------------------------------------------ 해도

  const PAD = 34;               // 여백(px)
  const MIN_SPAN_DEG = 0.04;    // 최소 표시 폭(도). 약 2.4 NM
  const FIT_MARGIN = 0.18;      // 점들 둘레 여유
  const GRID_STEPS = [0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5];

  class Chart {
    constructor(svg, options = {}) {
      this.svg = svg;
      this.onPick = options.onPick || null;
      this.showVector = Boolean(options.showVector);
      this.bounds = null;
      this.width = 0;
      this.height = 0;
      this.last = null;

      this.layers = {};
      for (const name of ["grid", "plan", "mroute", "pend", "track", "ship", "text", "scale"]) {
        this.layers[name] = svgEl("g", { class: `layer-${name}` });
        svg.appendChild(this.layers[name]);
      }

      // 자선 표식. 위치·침로만 바뀌므로 요소를 만들어 두고 transform만 갱신한다.
      this.ship = svgEl("g", { class: "ship" });
      this.shipRing = svgEl("circle", { class: "ring", r: 13 });
      this.shipVector = svgEl("line", { class: "vector", x1: 0, y1: -13, x2: 0, y2: -48 });
      this.shipHull = svgEl("polygon", { class: "hull", points: "0,-10 6,8 0,5 -6,8" });
      this.ship.append(this.shipRing, this.shipVector, this.shipHull);
      this.shipVector.style.display = this.showVector ? "" : "none";
      this.layers.ship.appendChild(this.ship);
      this.shipLabel = svgEl("text", { class: "ship-label" });
      this.layers.text.appendChild(this.shipLabel);

      svg.addEventListener("click", (event) => this._click(event));
      const observer = new ResizeObserver(() => this._resize());
      observer.observe(svg);
      this._resize();
    }

    _resize() {
      const rect = this.svg.getBoundingClientRect();
      this.width = Math.max(rect.width, 200);
      this.height = Math.max(rect.height, 200);
      this.svg.setAttribute("viewBox", `0 0 ${this.width} ${this.height}`);
      if (this.last) this.render(this.last);
    }

    // 점들이 모두 보이도록 경계를 잡는다. 이전 경계 안에 충분히 들어오면 유지해
    // 배가 움직일 때마다 축척이 흔들리지 않게 한다.
    _fit(points) {
      if (!points.length) return this.bounds;
      let lat0 = Infinity, lat1 = -Infinity, lon0 = Infinity, lon1 = -Infinity;
      for (const [lat, lon] of points) {
        if (lat < lat0) lat0 = lat; if (lat > lat1) lat1 = lat;
        if (lon < lon0) lon0 = lon; if (lon > lon1) lon1 = lon;
      }
      const cos = Math.cos(((lat0 + lat1) / 2) * Math.PI / 180);
      // 최소 폭 확보
      if (lat1 - lat0 < MIN_SPAN_DEG) {
        const c = (lat0 + lat1) / 2; lat0 = c - MIN_SPAN_DEG / 2; lat1 = c + MIN_SPAN_DEG / 2;
      }
      if ((lon1 - lon0) * cos < MIN_SPAN_DEG) {
        const c = (lon0 + lon1) / 2; lon0 = c - MIN_SPAN_DEG / cos / 2; lon1 = c + MIN_SPAN_DEG / cos / 2;
      }
      const b = this.bounds;
      if (b) {
        const inside = lat0 >= b.lat0 && lat1 <= b.lat1 && lon0 >= b.lon0 && lon1 <= b.lon1;
        const bigEnough =
          (lat1 - lat0) >= 0.3 * (b.lat1 - b.lat0) || (lon1 - lon0) >= 0.3 * (b.lon1 - b.lon0);
        if (inside && bigEnough) return b;
      }
      const dy = (lat1 - lat0) * FIT_MARGIN;
      const dx = (lon1 - lon0) * FIT_MARGIN;
      return { lat0: lat0 - dy, lat1: lat1 + dy, lon0: lon0 - dx, lon1: lon1 + dx };
    }

    // report.py _Projection과 같은 계산
    _setup(bounds) {
      const meanLat = ((bounds.lat0 + bounds.lat1) / 2) * Math.PI / 180;
      const cos = Math.cos(meanLat);
      const spanY = Math.max(bounds.lat1 - bounds.lat0, 1e-9);
      const spanX = Math.max((bounds.lon1 - bounds.lon0) * cos, 1e-9);
      const usableW = this.width - 2 * PAD;
      const usableH = this.height - 2 * PAD;
      const scale = Math.min(usableW / spanX, usableH / spanY);
      this.proj = {
        cos, scale,
        lon0: bounds.lon0, lat1: bounds.lat1,
        left: PAD + (usableW - spanX * scale) / 2,
        top: PAD + (usableH - spanY * scale) / 2,
      };
    }

    xy(lat, lon) {
      const p = this.proj;
      return [p.left + (lon - p.lon0) * p.cos * p.scale, p.top + (p.lat1 - lat) * p.scale];
    }

    latlon(x, y) {
      const p = this.proj;
      return [p.lat1 - (y - p.top) / p.scale, p.lon0 + (x - p.left) / (p.cos * p.scale)];
    }

    _click(event) {
      if (!this.onPick || !this.proj) return;
      const rect = this.svg.getBoundingClientRect();
      const [lat, lon] = this.latlon(event.clientX - rect.left, event.clientY - rect.top);
      this.onPick(Number(lat.toFixed(5)), Number(lon.toFixed(5)));
    }

    // data: {track, plan, mroute, mrouteNext, pend, pendFrom, ship:{lat,lon,heading,label,state}}
    render(data) {
      this.last = data;
      const all = [];
      for (const key of ["track", "plan", "mroute", "pend"]) for (const p of data[key] || []) all.push(p);
      if (data.ship) all.push([data.ship.lat, data.ship.lon]);
      if (!all.length) return;
      this.bounds = this._fit(all);
      this._setup(this.bounds);

      this._drawGrid();
      this._drawPolyline(this.layers.plan, data.plan || [], "plan-line", "plan-wp", "plan-label", "WP", 5);
      this._drawRoute(data.mroute || [], data.mrouteNext);
      this._drawPending(data.pend || [], data.pendFrom);
      this._drawTrack(data.track || []);
      this._drawShip(data.ship);
      this._drawScale();
    }

    _clear(layer) { while (layer.firstChild) layer.removeChild(layer.firstChild); }

    _drawGrid() {
      const layer = this.layers.grid;
      this._clear(layer);
      const [latBottom, lonLeft] = this.latlon(0, this.height);
      const [latTop, lonRight] = this.latlon(this.width, 0);
      const span = Math.max(latTop - latBottom, (lonRight - lonLeft) * this.proj.cos);
      const step = GRID_STEPS.find((s) => span / s <= 8) || 5;
      const decimals = step < 1 / 60 ? 1 : 0;

      const label = (value, isLat) => {
        const hemi = isLat ? (value >= 0 ? "N" : "S") : (value >= 0 ? "E" : "W");
        const abs = Math.abs(value);
        const deg = Math.floor(abs + 1e-9);
        const min = (abs - deg) * 60;
        return `${deg}°${min.toFixed(decimals).padStart(decimals ? 4 : 2, "0")}′${hemi}`;
      };

      for (let lat = Math.ceil(latBottom / step) * step; lat <= latTop; lat += step) {
        const [, y] = this.xy(lat, lonLeft);
        layer.appendChild(svgEl("line", { class: "grid-line", x1: 0, y1: y, x2: this.width, y2: y }));
        const t = svgEl("text", { class: "grid-label", x: 4, y: y - 3 });
        t.textContent = label(lat, true);
        layer.appendChild(t);
      }
      for (let lon = Math.ceil(lonLeft / step) * step; lon <= lonRight; lon += step) {
        const [x] = this.xy(latTop, lon);
        layer.appendChild(svgEl("line", { class: "grid-line", x1: x, y1: 0, x2: x, y2: this.height }));
        const t = svgEl("text", { class: "grid-label", x: x + 3, y: 12 });
        t.textContent = label(lon, false);
        layer.appendChild(t);
      }
    }

    _drawPolyline(layer, points, lineClass, wpClass, labelClass, prefix, r) {
      this._clear(layer);
      if (!points.length) return;
      const coords = points.map(([lat, lon]) => this.xy(lat, lon));
      if (coords.length > 1) {
        layer.appendChild(svgEl("polyline", { class: lineClass, points: coords.map((c) => c.join(",")).join(" ") }));
      }
      coords.forEach(([x, y], i) => {
        layer.appendChild(svgEl("circle", { class: wpClass, cx: x, cy: y, r }));
        const t = svgEl("text", { class: labelClass, x: x + 8, y: y - 6 });
        t.textContent = `${prefix}${i + 1}`;
        layer.appendChild(t);
      });
    }

    // MASS가 수락한 항로. 다음 목표 waypoint를 굵게 표시한다.
    _drawRoute(points, nextIndex) {
      const layer = this.layers.mroute;
      this._clear(layer);
      if (!points.length) return;
      const coords = points.map(([lat, lon]) => this.xy(lat, lon));
      if (coords.length > 1) {
        layer.appendChild(svgEl("polyline", { class: "mroute-line", points: coords.map((c) => c.join(",")).join(" ") }));
      }
      coords.forEach(([x, y], i) => {
        layer.appendChild(svgEl("circle", { class: i === nextIndex ? "mroute-wp next" : "mroute-wp", cx: x, cy: y, r: 5 }));
        const t = svgEl("text", { class: "plan-label", x: x + 8, y: y - 6, style: "fill:var(--mass)" });
        t.textContent = `WP${i + 1}`;
        layer.appendChild(t);
      });
    }

    // ROC에서 편집 중인 waypoint. 관측 위치에서 첫 점까지 점선으로 잇는다.
    _drawPending(points, from) {
      const layer = this.layers.pend;
      this._clear(layer);
      if (!points.length) return;
      const coords = points.map(([lat, lon]) => this.xy(lat, lon));
      const line = from ? [this.xy(from[0], from[1]), ...coords] : coords;
      if (line.length > 1) {
        layer.appendChild(svgEl("polyline", { class: "pend-line", points: line.map((c) => c.join(",")).join(" ") }));
      }
      coords.forEach(([x, y], i) => {
        layer.appendChild(svgEl("circle", { class: "pend-wp", cx: x, cy: y, r: 4 }));
        const t = svgEl("text", { class: "pend-label", x: x + 7, y: y + 12 });
        t.textContent = String(i + 1);
        layer.appendChild(t);
      });
    }

    _drawTrack(points) {
      const layer = this.layers.track;
      this._clear(layer);
      if (!points.length) return;
      const coords = points.map(([lat, lon]) => this.xy(lat, lon));
      if (coords.length > 1) {
        layer.appendChild(svgEl("polyline", { class: "track-line", points: coords.map((c) => c.join(",")).join(" ") }));
      }
      layer.appendChild(svgEl("circle", { class: "track-start", cx: coords[0][0], cy: coords[0][1], r: 3 }));
    }

    _drawShip(ship) {
      if (!ship) { this.ship.style.display = "none"; this.shipLabel.style.display = "none"; return; }
      this.ship.style.display = "";
      this.shipLabel.style.display = "";
      const [x, y] = this.xy(ship.lat, ship.lon);
      this.ship.setAttribute("transform", `translate(${x.toFixed(1)} ${y.toFixed(1)}) rotate(${(ship.heading || 0).toFixed(1)})`);
      this.ship.setAttribute("class", `ship ${ship.state || ""}`);
      this.shipLabel.setAttribute("x", (x + 16).toFixed(1));
      this.shipLabel.setAttribute("y", (y + 4).toFixed(1));
      this.shipLabel.setAttribute("class", `ship-label ${ship.state || ""}`);
      this.shipLabel.textContent = ship.label || "";
    }

    // 오른쪽 아래 축척 막대 (1 NM = 1/60도 위도)
    _drawScale() {
      const layer = this.layers.scale;
      this._clear(layer);
      const pxPerNm = this.proj.scale / 60;
      const nm = [0.1, 0.25, 0.5, 1, 2, 5, 10].find((n) => n * pxPerNm >= 60) || 10;
      const w = nm * pxPerNm;
      const x1 = this.width - 14 - w, y = this.height - 30;
      layer.appendChild(svgEl("line", { x1, y1: y, x2: x1 + w, y2: y, stroke: "var(--dim)", "stroke-width": 1.5 }));
      layer.appendChild(svgEl("line", { x1, y1: y - 4, x2: x1, y2: y + 4, stroke: "var(--dim)" }));
      layer.appendChild(svgEl("line", { x1: x1 + w, y1: y - 4, x2: x1 + w, y2: y + 4, stroke: "var(--dim)" }));
      const t = svgEl("text", { class: "grid-label", x: x1 + w / 2, y: y - 6, "text-anchor": "middle" });
      t.textContent = `${nm} NM`;
      layer.appendChild(t);
    }
  }

  // ------------------------------------------------------------ 공통 상단

  function renderTopbar(snap) {
    $("#sim-time").textContent = fmtTime(snap.t);
    const pill = $("#link-pill");
    pill.className = `pill ${snap.link_up ? "up" : "down"}`;
    pill.lastElementChild.textContent = snap.link_up ? "LINK" : "LINK LOST";
  }

  function statusPill(text, cls) {
    return el("span", { class: `status ${cls}`, text });
  }

  function renderLog(list, entries) {
    list.replaceChildren(
      ...entries.slice().reverse().map((entry) => {
        const cls = entry.kind === "reject" ? "reject" : entry.kind === "lost" || entry.kind === "link" ? "lost" : "";
        const arrow = entry.to === "—" ? entry.from : `${entry.from} → ${entry.to}`;
        return el("li", { class: cls }, [
          el("span", { class: "t", text: fmtTime(entry.t) }),
          el("span", { class: "txt" }, [
            el("div", { text: entry.text }),
            el("div", { class: "sub", text: arrow }),
          ]),
          el("span", {}, [statusPill(entry.kind.toUpperCase(),
            { send: "neutral", ack: "ok", reject: "bad", lost: "lost", link: "lost" }[entry.kind] || "neutral")]),
        ]);
      })
    );
  }

  // ------------------------------------------------------------ ROC 콘솔

  function setupRoc() {
    const wps = [];                 // 편집 중인 waypoint [{lat, lon}]
    const observed = [];            // ROC가 수신한 위치들 (관측 항적)
    let lastUpdatedAt = null;
    let activeTab = "route";
    let snap = null;

    const chart = new Chart($("#chart"), {
      onPick: (lat, lon) => {
        if (activeTab !== "route" || wps.length >= 16) return;
        wps.push({ lat, lon });
        renderWps();
      },
    });

    // --- 탭
    for (const button of document.querySelectorAll(".tabs button")) {
      button.addEventListener("click", () => {
        activeTab = button.dataset.tab;
        for (const b of document.querySelectorAll(".tabs button")) b.setAttribute("aria-selected", String(b === button));
        for (const pane of document.querySelectorAll(".tab-pane")) pane.classList.toggle("active", pane.dataset.pane === activeTab);
        $("#chart-wrap").classList.toggle("pickable", activeTab === "route");
      });
    }

    // --- waypoint 편집
    function renderWps() {
      const list = $("#wp-list");
      list.replaceChildren(
        ...wps.map((wp, i) => {
          const lat = el("input", { type: "number", step: "0.00001", value: wp.lat, "aria-label": `WP${i + 1} 위도` });
          const lon = el("input", { type: "number", step: "0.00001", value: wp.lon, "aria-label": `WP${i + 1} 경도` });
          lat.addEventListener("input", () => { wp.lat = Number(lat.value); });
          lon.addEventListener("input", () => { wp.lon = Number(lon.value); });
          const rm = el("button", { class: "rm", text: "×", title: "삭제" });
          rm.addEventListener("click", () => { wps.splice(i, 1); renderWps(); });
          return el("div", { class: "wp-row" }, [el("span", { class: "idx", text: `WP${i + 1}` }), lat, lon, rm]);
        })
      );
      if (!wps.length) list.appendChild(el("p", { class: "empty", text: "웨이포인트가 없습니다. 해도를 클릭하세요." }));
    }
    $("#btn-wp-add").addEventListener("click", () => {
      if (wps.length >= 16) return;
      const base = wps[wps.length - 1] || observedShip() || { lat: 35.06, lon: 129.12 };
      wps.push({ lat: Number((base.lat + 0.02).toFixed(5)), lon: Number((base.lon + 0.03).toFixed(5)) });
      renderWps();
    });
    $("#btn-wp-clear").addEventListener("click", () => { wps.length = 0; renderWps(); });
    renderWps();

    // --- 발행
    const flash = (id, text, cls) => {
      const node = $(id);
      node.textContent = text;
      node.className = `msg ${cls || ""}`;
    };

    $("#btn-route").addEventListener("click", async () => {
      const body = {
        kind: "route",
        waypoints: wps.map((wp) => [wp.lat, wp.lon]),
        route_type: Number($("#route-type").value),
      };
      const duration = $("#route-duration").value.trim();
      if (duration) body.duration = Number(duration);
      try {
        const result = await api("/api/command", body);
        flash("#msg-route", `발행됨 · Linkage ${result.linkage_id}`, "ok");
        wps.length = 0;
        renderWps();
      } catch (error) {
        flash("#msg-route", error.message, "err");
      }
    });

    const headingInput = $("#ctl-heading");
    const updatePreview = () => { $("#ctl-preview").textContent = fmtHeading(Number(headingInput.value) || 0); };
    headingInput.addEventListener("input", updatePreview);
    updatePreview();

    $("#btn-control").addEventListener("click", async () => {
      const body = { kind: "control", heading: Number(headingInput.value), fallback: Number($("#ctl-fallback").value) };
      const sog = $("#ctl-sog").value.trim();
      if (sog) body.sog = Number(sog);
      try {
        const result = await api("/api/command", body);
        flash("#msg-control", `발행됨 · Linkage ${result.linkage_id}`, "ok");
      } catch (error) {
        flash("#msg-control", error.message, "err");
      }
    });

    $("#btn-cancel").addEventListener("click", async () => {
      try {
        const result = await api("/api/command", { kind: "cancel" });
        flash("#msg-cancel", `취소 발행됨 · Linkage ${result.linkage_id}`, "ok");
      } catch (error) {
        flash("#msg-cancel", error.message, "err");
      }
    });

    $("#btn-reset").addEventListener("click", async () => {
      await api("/api/reset", {});
      observed.length = 0; lastUpdatedAt = null; wps.length = 0; renderWps();
      chart.bounds = null;
    });

    function observedShip() {
      const track = snap && snap.roc.tracks.find((t) => t.source_id === snap.mass.mmsi);
      return track && track.lat != null ? track : null;
    }

    // --- 렌더
    function render(next) {
      snap = next;
      renderTopbar(snap);
      $("#own-mmsi").textContent = String(snap.roc.mmsi);

      // 관측 항적 누적: 새 위치보고가 들어왔을 때만 점을 더한다.
      const own = observedShip();
      if (own && own.updated_at !== lastUpdatedAt) {
        lastUpdatedAt = own.updated_at;
        const prev = observed[observed.length - 1];
        if (!prev || prev[0] !== own.lat || prev[1] !== own.lon) observed.push([own.lat, own.lon]);
        if (observed.length > 600) observed.splice(0, observed.length - 600);
      }

      // 추적 목록
      const tbody = $("#tracks");
      tbody.replaceChildren(
        ...snap.roc.tracks.map((track) => {
          const [modeShort] = MODE_TEXT[track.autonomy_mode] || MODE_TEXT[0];
          return el("tr", {}, [
            el("td", { class: "mono", text: String(track.source_id) }),
            el("td", {}, [el("span", { class: `mode-badge mode-${track.autonomy_mode}`, text: modeShort, style: "padding:1px 7px;font-size:11px" })]),
            el("td", { class: "num", text: `${fmtTime(track.updated_at)}  (${track.age.toFixed(0)}s 전)` }),
            el("td", { class: "num", text: String(track.report_count) }),
            el("td", {}, [
              track.lost ? statusPill("두절", "lost")
                : track.age > snap.roc.report_timeout / 2 ? statusPill("지연", "wait")
                : statusPill("정상", "ok"),
            ]),
          ]);
        })
      );
      $("#track-count").textContent = snap.roc.tracks.length ? String(snap.roc.tracks.length) : "";
      $("#tracks-hint").textContent = snap.roc.tracks.length
        ? `위치보고가 ${snap.roc.report_timeout.toFixed(0)}초 이상 없으면 두절로 판정합니다. 모드는 본선이 보고한 값입니다.`
        : "아직 위치보고가 없습니다.";

      // 명령 이력
      $("#commands").replaceChildren(
        ...snap.roc.commands.slice().reverse().map((cmd) => {
          const pill = cmd.superseded ? statusPill("재발행됨", "neutral")
            : !cmd.answered ? statusPill("대기중", "wait")
            : cmd.accepted ? statusPill(cmd.status_name, "ok")
            : statusPill(`${cmd.status_name} · ${cmd.reason_name}`, "bad");
          return el("tr", {}, [
            el("td", { class: "num", text: String(cmd.linkage_id) }),
            el("td", { class: "num", text: fmtTime(cmd.issued_at) }),
            el("td", { text: cmd.text }),
            el("td", {}, [pill]),
          ]);
        })
      );
      $("#cmd-count").textContent = snap.roc.commands.length ? String(snap.roc.commands.length) : "";
      const current = snap.roc.current_linkage_id;
      $("#cancel-box").innerHTML = current == null
        ? "취소할 항로가 없습니다. 먼저 노선이나 조종을 발행하세요."
        : `현재 항로 Linkage ID <b>${current}</b> 에 대해 취소(Route Type 31)를 보냅니다.`;

      renderLog($("#log"), snap.log);

      chart.render({
        track: observed,
        plan: snap.roc.planned_route,
        pend: wps.map((wp) => [wp.lat, wp.lon]),
        pendFrom: own ? [own.lat, own.lon] : null,
        ship: own ? {
          lat: own.lat, lon: own.lon, heading: own.cog || 0,
          label: `${own.source_id} · ${own.autonomy_name}`,
          state: own.lost ? "lost" : own.age > snap.roc.report_timeout / 2 ? "stale" : "",
        } : null,
      });
    }

    return render;
  }

  // ------------------------------------------------------------ MASS 콘솔

  function setupMass() {
    const chart = new Chart($("#chart"), { showVector: true });
    let linkUp = true;

    $("#btn-link").addEventListener("click", async () => {
      await api("/api/link", { up: !linkUp });
    });
    $("#btn-reset").addEventListener("click", async () => {
      await api("/api/reset", {});
      chart.bounds = null;
    });

    function render(snap) {
      renderTopbar(snap);
      const m = snap.mass;
      $("#own-mmsi").textContent = String(m.mmsi);

      $("#st-lat").textContent = fmtDMM(m.lat, true);
      $("#st-lon").textContent = fmtDMM(m.lon, false);
      $("#st-hdg").textContent = fmtHeading(m.heading);
      $("#st-sog").innerHTML = `${fmtKnots(m.sog)}<small>kn</small>`;
      const [short, long] = MODE_TEXT[m.autonomy_mode] || MODE_TEXT[0];
      const badge = $("#st-mode");
      badge.className = `mode-badge mode-${m.autonomy_mode}`;
      badge.replaceChildren(document.createTextNode(short), el("small", { text: long }));

      $("#rt-remaining").textContent = m.has_route ? String(m.remaining_waypoints) : "—";
      $("#rt-linkage").textContent = m.route_linkage_id ? String(m.route_linkage_id) : "—";
      $("#rt-next").textContent = m.next_waypoint
        ? `${fmtDMM(m.next_waypoint.lat, true)}  ${fmtDMM(m.next_waypoint.lon, false)}`
        : "항로 없음";
      const controlWrap = $("#rt-control-wrap");
      if (m.control) {
        controlWrap.hidden = false;
        $("#rt-control").textContent = `HDG ${fmtHeading(m.control.heading)} · Fallback ${m.control.fallback_name}`;
      } else {
        controlWrap.hidden = true;
      }

      $("#rx-log").replaceChildren(
        ...m.log.slice().reverse().map((entry) => {
          const cls = [entry.accepted ? "" : "reject", entry.from !== "ROC" ? "foreign" : ""].join(" ").trim();
          return el("li", { class: cls }, [
            el("span", { class: "t", text: fmtTime(entry.t) }),
            el("span", { class: "txt" }, [
              el("div", { text: entry.text }),
              el("div", { class: "sub", text: `송신국 ${entry.from}${entry.linkage_id != null ? ` · Linkage ${entry.linkage_id}` : ""}` }),
            ]),
            el("span", {}, [
              entry.accepted ? statusPill("수락", "ok")
                : statusPill(`거절 · ${entry.status_name}${entry.reason_name && entry.reason_name !== "none" ? ` (${entry.reason_name})` : ""}`, "bad"),
            ]),
          ]);
        })
      );
      $("#rx-count").textContent = m.log.length ? String(m.log.length) : "";
      if (!m.log.length) $("#rx-log").appendChild(el("li", { class: "empty", text: "아직 받은 명령이 없습니다." }));

      $("#ct-rejected").textContent = String(m.rejected_count);
      $("#ct-rejected").className = `v${m.rejected_count ? " alert" : ""}`;
      $("#ct-ignored").textContent = String(m.ignored_count);
      $("#ct-ignored").className = `v${m.ignored_count ? " alert" : ""}`;
      $("#ct-dropped").textContent = String(snap.dropped);
      $("#ct-dropped").className = `v${snap.dropped ? " alert" : ""}`;

      linkUp = snap.link_up;
      const linkButton = $("#btn-link");
      linkButton.textContent = linkUp ? "링크 두절" : "링크 복구";
      linkButton.className = `btn ${linkUp ? "danger" : "primary"}`;

      const nextIndex = m.has_route ? m.route.length - m.remaining_waypoints : -1;
      chart.render({
        track: m.track,
        mroute: m.route,
        mrouteNext: nextIndex,
        ship: { lat: m.lat, lon: m.lon, heading: m.heading, label: `${short} · ${fmtKnots(m.sog)} kn`, state: "" },
      });
    }

    return render;
  }

  // ------------------------------------------------------------ 폴링

  const render = PAGE === "roc" ? setupRoc() : setupMass();
  let busy = false;

  async function poll() {
    if (busy) return;
    busy = true;
    try {
      const snap = await api("/api/state");
      render(snap);
      document.title = document.title.replace(/^\[연결 끊김\] /, "");
    } catch (error) {
      if (!document.title.startsWith("[연결 끊김]")) document.title = `[연결 끊김] ${document.title}`;
    } finally {
      busy = false;
    }
  }

  poll();
  setInterval(poll, POLL_MS);
})();
