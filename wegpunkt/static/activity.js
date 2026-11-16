// Wegpunkt: Karte und Verlaufsdiagramm einer GPS-Aktivität
(() => {
  "use strict";
  const dataEl = document.getElementById("track-data");
  const card = document.querySelector(".map-card");
  if (!dataEl || !card || !window.L) return;

  const P = JSON.parse(dataEl.textContent);  // [lat, lon, km, höhe, sekunden, puls]
  if (P.length < 2) return;
  const fmt = (n, d = 0) => n.toLocaleString("de-DE", { minimumFractionDigits: d, maximumFractionDigits: d });
  const css = (name) => getComputedStyle(card).getPropertyValue(name).trim();
  const isDark = () => {
    const t = document.documentElement.dataset.theme;
    return t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  };

  // Karte ---------------------------------------------------------------------
  const map = L.map("map", { scrollWheelZoom: false, zoomSnap: 0.25 });
  map.attributionControl.setPrefix('<a href="https://leafletjs.com">Leaflet</a>');
  const osm = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>-Mitwirkende';
  const layers = {
    // Im dunklen Farbschema färbt das Stylesheet diese Kacheln um (Klasse base-tiles).
    map: L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
      { maxZoom: 19, className: "base-tiles", attribution: osm }),
    topo: L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
      { subdomains: "abc", maxZoom: 17, attribution: `${osm}, SRTM | &copy; <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA)` }),
  };
  let active = card.dataset.defaultLayer || "map";
  layers[active].addTo(map);
  const buttons = card.querySelectorAll("[data-layer]");
  const mark = () => buttons.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.layer === active)));
  buttons.forEach((b) => b.addEventListener("click", () => {
    map.removeLayer(layers[active]);
    active = b.dataset.layer;
    layers[active].addTo(map).bringToBack();
    mark();
  }));
  mark();
  // Mit Klick in die Karte darf das Mausrad zoomen
  map.on("click", () => map.scrollWheelZoom.enable());
  map.getContainer().addEventListener("mouseleave", () => map.scrollWheelZoom.disable());

  const ll = P.map((p) => [p[0], p[1]]);
  const casing = L.polyline(ll, { weight: 9, opacity: 0.85, interactive: false }).addTo(map);
  const line = L.polyline(ll, { weight: 4.5, lineJoin: "round" }).addTo(map);
  const dot = (latlng) => L.circleMarker(latlng, { radius: 6.5, color: "#fff", weight: 3, fillOpacity: 1 }).addTo(map);
  const startDot = dot(ll[0]), endDot = dot(ll[ll.length - 1]);
  const cursor = L.circleMarker(ll[0], { radius: 8, color: "#fff", weight: 3, fillOpacity: 0, opacity: 0, interactive: false }).addTo(map);
  map.fitBounds(line.getBounds(), { padding: [32, 32] });

  let color = css("--sport");
  const paint = () => {
    color = css("--sport") || "#e85414";
    casing.setStyle({ color: isDark() ? "#0d0f12" : "#ffffff" });
    line.setStyle({ color });
    startDot.setStyle({ fillColor: "#1b1e23" });
    endDot.setStyle({ fillColor: color });
    cursor.setStyle({ fillColor: color });
  };
  paint();
  const showCursor = (i) => {
    if (i == null) { cursor.setStyle({ opacity: 0, fillOpacity: 0 }); return; }
    cursor.setLatLng(ll[i]).setStyle({ opacity: 1, fillOpacity: 1 });
  };

  // Verlauf -------------------------------------------------------------------
  const panel = document.querySelector(".chart-panel");
  const box = document.getElementById("chart");
  const readout = document.getElementById("readout");
  let setCursor = () => {};

  if (panel && box) {
    const pace = panel.dataset.metric === "pace";
    const maxKm = P[P.length - 1][2] || 1;

    // Geschwindigkeit in km/h, über ein Fenster von etwa 20 Sekunden geglättet
    const speed = P.map(() => null);
    if (P.every((p) => p[4] != null)) {
      let a = 0, b = 0;
      for (let i = 0; i < P.length; i++) {
        while (a < i && P[i][4] - P[a + 1][4] >= 10) a++;
        while (b < P.length - 1 && P[b][4] - P[i][4] < 10) b++;
        const dt = P[b][4] - P[a][4];
        if (dt > 0) speed[i] = Math.min((P[b][2] - P[a][2]) / dt * 3600, 120);
      }
    }
    const paceText = (kmh) => {
      if (!kmh || kmh < 2) return "–";
      const s = Math.round(3600 / kmh);
      return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    };
    const series = {
      ele: { values: P.map((p) => p[3]), label: (v) => `${fmt(v)} m`, pad: 5 },
      speed: { values: speed, label: (v) => (pace ? `${paceText(v)} /km` : `${fmt(v, 1)} km/h`), pad: 1, floor: 0 },
      hr: { values: P.map((p) => p[5] || null), label: (v) => `${fmt(v)} bpm`, pad: 5 },
    };
    const tabs = [...panel.querySelectorAll("[data-series]")];
    let current = tabs[0]?.dataset.series;
    let points = [];  // Indizes in P mit Wert in der aktuellen Reihe

    const W = 1000, H = 200, top = 14, bottom = 26;
    const x = (km) => km / maxKm * W;

    const draw = () => {
      const s = series[current];
      points = P.map((_, i) => i).filter((i) => s.values[i] != null);
      if (points.length < 2) { box.innerHTML = ""; return; }
      const vals = points.map((i) => s.values[i]);
      // Ausreißer oben kappen, damit ein GPS-Sprung nicht alles staucht
      const sorted = [...vals].sort((p, q) => p - q);
      const cap = current === "speed" ? sorted[Math.floor(sorted.length * 0.995)] * 1.05 : Infinity;
      const vMin = Math.min(...vals), vMax = Math.min(Math.max(...vals), cap);
      const pad = Math.max((vMax - vMin) * 0.12, s.pad);
      const lo = s.floor != null ? Math.max(s.floor, vMin - pad) : vMin - pad, hi = vMax + pad;
      const y = (v) => top + (1 - (Math.min(v, hi) - lo) / (hi - lo)) * (H - top - bottom);
      const pts = points.map((i) => `${x(P[i][2]).toFixed(1)},${y(s.values[i]).toFixed(1)}`);
      const first = x(P[points[0]][2]), last = x(P[points[points.length - 1]][2]);
      // Beschriftung als HTML, damit sie beim Strecken des Diagramms nicht verzerrt
      const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) =>
        `<span class="tick" style="left:${f * 100}%" data-pos="${f}">${fmt(maxKm * f, maxKm < 10 ? 1 : 0)} km</span>`).join("");
      box.innerHTML = `
        <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img"
             aria-label="Verlauf zwischen ${s.label(vMin)} und ${s.label(vMax)}">
          <defs><linearGradient id="chart-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color="${color}" stop-opacity=".38"/><stop offset="1" stop-color="${color}" stop-opacity=".02"/>
          </linearGradient></defs>
          <line class="grid" x1="0" x2="${W}" y1="${y(vMax)}" y2="${y(vMax)}"/>
          <line class="grid" x1="0" x2="${W}" y1="${y(vMin)}" y2="${y(vMin)}"/>
          <polygon points="${first},${H - bottom} ${pts.join(" ")} ${last},${H - bottom}" fill="url(#chart-fill)"/>
          <polyline points="${pts.join(" ")}" fill="none" stroke="${color}" stroke-width="2.2" vector-effect="non-scaling-stroke" stroke-linejoin="round"/>
          <line class="crosshair" x1="0" x2="0" y1="${top - 6}" y2="${H - bottom}" vector-effect="non-scaling-stroke"/>
        </svg>
        ${ticks}
        <span class="elev-label" style="top:${y(vMax) / H * 100}%">${s.label(vMax)}</span>
        <span class="elev-label" style="top:${y(vMin) / H * 100}%">${s.label(vMin)}</span>
        <span class="elev-dot"></span>`;
      const svg = box.querySelector("svg"), cross = box.querySelector(".crosshair"), edot = box.querySelector(".elev-dot");
      const idle = `${s.label(vMin)} – ${s.label(vMax)}`;
      readout.textContent = idle;

      setCursor = (i) => {
        if (i == null) { box.classList.remove("is-active"); readout.textContent = idle; showCursor(null); return; }
        const p = P[i], v = s.values[i];
        cross.setAttribute("x1", x(p[2])); cross.setAttribute("x2", x(p[2]));
        edot.style.left = `${x(p[2]) / W * 100}%`;
        edot.style.top = `${(v != null ? y(v) : H - bottom) / H * 100}%`;
        box.classList.add("is-active");
        const parts = [`${fmt(p[2], 2)} km`];
        for (const key of ["ele", "speed", "hr"]) {
          const value = series[key].values[i];
          if (value != null && tabs.some((t) => t.dataset.series === key)) parts.push(series[key].label(value));
        }
        readout.textContent = parts.join(" · ");
        showCursor(i);
      };
      const nearest = (km) => {
        let a = 0, b = points.length - 1;
        while (b - a > 1) { const m = (a + b) >> 1; if (P[points[m]][2] < km) a = m; else b = m; }
        return Math.abs(P[points[a]][2] - km) <= Math.abs(P[points[b]][2] - km) ? points[a] : points[b];
      };
      const onMove = (e) => {
        const r = svg.getBoundingClientRect();
        setCursor(nearest(Math.min(Math.max((e.clientX - r.left) / r.width, 0), 1) * maxKm));
      };
      svg.addEventListener("pointermove", onMove);
      svg.addEventListener("pointerdown", onMove);
      svg.addEventListener("pointerleave", () => setCursor(null));
    };

    const select = (name) => {
      current = name;
      tabs.forEach((t) => t.setAttribute("aria-selected", String(t.dataset.series === name)));
      draw();
    };
    tabs.forEach((t) => t.addEventListener("click", () => select(t.dataset.series)));
    if (tabs.length < 2) panel.classList.add("single-series");
    if (current) select(current);

    line.on("mousemove", (e) => {
      let best = 0, bestD = Infinity;
      for (const i of points) {
        const d = (P[i][0] - e.latlng.lat) ** 2 + (P[i][1] - e.latlng.lng) ** 2;
        if (d < bestD) { bestD = d; best = i; }
      }
      setCursor(best);
    });
    line.on("mouseout", () => setCursor(null));

    document.addEventListener("themechange", () => { paint(); draw(); });
  } else {
    document.addEventListener("themechange", paint);
  }

})();
