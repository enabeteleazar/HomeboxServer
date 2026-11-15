/* Historique : graphiques en ligne (SVG, sans bibliothèque).
 *
 * Chaque [data-metric-chart] décrit ses séries en JSON (clé, libellé, couleur).
 * Les données viennent de /history/data?range=… ; un changement de période
 * recharge tous les graphiques (le dessin précédent reste affiché, estompé,
 * pendant le chargement). Crosshair + infobulle au survol et au clavier,
 * tableau des données sous chaque graphique.
 */
(function () {
  "use strict";

  var root = document.querySelector("[data-history]");
  if (!root || typeof fetch !== "function") return;

  var SVG = "http://www.w3.org/2000/svg";
  var tooltip = document.getElementById("chart-tooltip");
  var state = { range: root.getAttribute("data-range") || "24h", data: null, timer: null };

  /* --- formats ----------------------------------------------------------- */

  function fmtNumber(v, digits) {
    return v.toLocaleString("fr-FR", { maximumFractionDigits: digits, minimumFractionDigits: 0 });
  }

  function fmtBytes(v) {
    var units = ["o/s", "Ko/s", "Mo/s", "Go/s"];
    var i = 0;
    while (Math.abs(v) >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return fmtNumber(v, v >= 100 || i === 0 ? 0 : 1) + " " + units[i];
  }

  var FORMATS = {
    pct: function (v) { return fmtNumber(v, v >= 10 ? 0 : 1) + " %"; },
    temp: function (v) { return fmtNumber(v, 0) + " °C"; },
    rate: fmtBytes,
    load: function (v) { return fmtNumber(v, 2); }
  };

  function fmtTime(ts, withDay) {
    var d = new Date(ts * 1000);
    var hm = d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
    if (!withDay) return hm;
    return d.toLocaleDateString("fr-FR", { day: "2-digit", month: "short" }) + " " + hm;
  }

  function fmtAxisTime(ts, span) {
    var d = new Date(ts * 1000);
    if (span <= 86400) return d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
    return d.toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" });
  }

  /* --- échelle ----------------------------------------------------------- */

  function niceStep(max, count) {
    var raw = max / count;
    var mag = Math.pow(10, Math.floor(Math.log10(raw)));
    var norm = raw / mag;
    var step = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10;
    return step * mag;
  }

  function byteStep(max, count) {
    // graduations « rondes » en puissances de 1024 pour les débits
    var unit = 1;
    while (max / unit >= 1024) unit *= 1024;
    return niceStep(max / unit, count) * unit;
  }

  function el(name, attrs, parent) {
    var node = document.createElementNS(SVG, name);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    if (parent) parent.appendChild(node);
    return node;
  }

  /* --- dessin d'un graphique ---------------------------------------------- */

  function draw(container) {
    var data = state.data;
    var cfg = JSON.parse(container.getAttribute("data-metric-chart"));
    var fmt = FORMATS[cfg.format] || FORMATS.pct;
    var points = data.points;
    container.textContent = "";

    if (!points.length) {
      var empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "Pas encore de mesures sur cette période — le collecteur enregistre toutes les 15 s.";
      container.appendChild(empty);
      return;
    }

    var width = Math.max(260, container.clientWidth);
    var height = 190;
    var m = { top: 10, right: 12, bottom: 24, left: cfg.format === "rate" ? 62 : 44 };
    var w = width - m.left - m.right;
    var h = height - m.top - m.bottom;

    // borne haute : fixe pour les pourcentages, sinon d'après les pics
    var top = cfg.max || 0;
    if (!cfg.max) {
      points.forEach(function (p) {
        cfg.series.forEach(function (s) {
          var v = data.aggregated && p[s.key + "_max"] != null ? p[s.key + "_max"] : p[s.key];
          if (v != null && v > top) top = v;
        });
      });
      top = top > 0 ? top * 1.1 : 1;
    }
    var step = cfg.format === "rate" ? byteStep(top, 4) : niceStep(top, 4);
    if (!cfg.max) top = Math.ceil(top / step) * step;

    var t0 = data.start, t1 = data.end, span = t1 - t0;
    function x(t) { return m.left + ((t - t0) / span) * w; }
    function y(v) { return m.top + h - (Math.min(v, top) / top) * h; }

    var svg = el("svg", { viewBox: "0 0 " + width + " " + height, role: "img",
      "aria-label": cfg.title + " — " + (data.range) });
    container.appendChild(svg);

    // quadrillage horizontal + graduations
    for (var v = 0; v <= top + 1e-9; v += step) {
      el("line", { x1: m.left, x2: m.left + w, y1: y(v), y2: y(v), class: v === 0 ? "axis-line" : "grid-line" }, svg);
      var tick = el("text", { x: m.left - 6, y: y(v) + 3, "text-anchor": "end", class: "tick" }, svg);
      tick.textContent = fmt(v);
    }

    // graduations de temps
    var ticks = 5;
    for (var i = 0; i <= ticks; i++) {
      var tt = t0 + (span * i) / ticks;
      var label = el("text", { x: x(tt), y: height - 6, "text-anchor": i === 0 ? "start" : i === ticks ? "end" : "middle", class: "tick" }, svg);
      label.textContent = fmtAxisTime(tt, span);
    }

    // segments continus : une interruption de collecte coupe la ligne
    var gap = data.step * 2.5;
    function segments(key) {
      var out = [], cur = [];
      points.forEach(function (p, idx) {
        var prev = points[idx - 1];
        if (p[key] == null || (prev && p.t - prev.t > gap)) {
          if (cur.length) out.push(cur);
          cur = [];
        }
        if (p[key] != null) cur.push(p);
      });
      if (cur.length) out.push(cur);
      return out;
    }

    cfg.series.forEach(function (s) {
      // bande des pics (période regroupée) : un voile de la couleur de la série
      if (data.aggregated && s.peak !== false) {
        segments(s.key).forEach(function (seg) {
          if (seg.length < 2) return;
          var d = seg.map(function (p, j) { return (j ? "L" : "M") + x(p.t) + " " + y(p[s.key + "_max"] != null ? p[s.key + "_max"] : p[s.key]); }).join(" ");
          d += seg.slice().reverse().map(function (p) { return " L" + x(p.t) + " " + y(p[s.key]); }).join("") + " Z";
          el("path", { d: d, fill: s.color, "fill-opacity": 0.1, stroke: "none" }, svg);
        });
      }
      segments(s.key).forEach(function (seg) {
        var d = seg.map(function (p, j) { return (j ? "L" : "M") + x(p.t) + " " + y(p[s.key]); }).join(" ");
        el("path", { d: d, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
      });
      // point final (dernière valeur), anneau couleur de fond
      for (var k = points.length - 1; k >= 0; k--) {
        if (points[k][s.key] != null) {
          el("circle", { cx: x(points[k].t), cy: y(points[k][s.key]), r: 4, fill: s.color, stroke: "var(--surface)", "stroke-width": 2 }, svg);
          break;
        }
      }
    });

    // crosshair + infobulle (souris et clavier)
    var cross = el("line", { y1: m.top, y2: m.top + h, class: "axis-line", visibility: "hidden" }, svg);
    var dots = cfg.series.map(function (s) {
      return el("circle", { r: 4, fill: s.color, stroke: "var(--surface)", "stroke-width": 2, visibility: "hidden" }, svg);
    });
    var hit = el("rect", { x: m.left, y: m.top, width: w, height: h, fill: "transparent", tabindex: 0,
      "aria-label": "Parcourir " + cfg.title + " avec les flèches" }, svg);
    var current = points.length - 1;

    function show(index, clientX, clientY) {
      current = Math.max(0, Math.min(points.length - 1, index));
      var p = points[current];
      cross.setAttribute("x1", x(p.t)); cross.setAttribute("x2", x(p.t));
      cross.setAttribute("visibility", "visible");
      cfg.series.forEach(function (s, j) {
        if (p[s.key] == null) { dots[j].setAttribute("visibility", "hidden"); return; }
        dots[j].setAttribute("cx", x(p.t)); dots[j].setAttribute("cy", y(p[s.key]));
        dots[j].setAttribute("visibility", "visible");
      });
      if (!tooltip) return;
      tooltip.textContent = "";
      var title = document.createElement("strong");
      title.textContent = fmtTime(p.t, span > 86400) + (data.aggregated ? " (moyenne)" : "");
      tooltip.appendChild(title);
      cfg.series.forEach(function (s) {
        var row = document.createElement("div");
        row.className = "row";
        var name = document.createElement("span");
        var key = document.createElement("i");
        key.className = "line-key";
        key.style.background = s.color;
        name.appendChild(key);
        name.appendChild(document.createTextNode(s.label));
        var value = document.createElement("span");
        value.textContent = p[s.key] == null ? "—" : fmt(p[s.key]) +
          (data.aggregated && p[s.key + "_max"] != null && s.peak !== false ? "  · pic " + fmt(p[s.key + "_max"]) : "");
        row.appendChild(name); row.appendChild(value);
        tooltip.appendChild(row);
      });
      tooltip.hidden = false;
      var rect = svg.getBoundingClientRect();
      var px = clientX != null ? clientX : rect.left + (x(p.t) / width) * rect.width;
      var py = clientY != null ? clientY : rect.top + 20;
      var box = tooltip.getBoundingClientRect();
      tooltip.style.left = Math.max(8, Math.min(px + 14, window.innerWidth - box.width - 8)) + "px";
      tooltip.style.top = Math.max(8, py - box.height - 12) + "px";
    }

    function hide() {
      cross.setAttribute("visibility", "hidden");
      dots.forEach(function (d) { d.setAttribute("visibility", "hidden"); });
      if (tooltip) tooltip.hidden = true;
    }

    function nearest(clientX) {
      var rect = svg.getBoundingClientRect();
      var t = t0 + (((clientX - rect.left) / rect.width) * width - m.left) / w * span;
      var best = 0, bestGap = Infinity;
      points.forEach(function (p, idx) {
        var g = Math.abs(p.t - t);
        if (g < bestGap) { bestGap = g; best = idx; }
      });
      return best;
    }

    hit.addEventListener("pointermove", function (e) { show(nearest(e.clientX), e.clientX, e.clientY); });
    hit.addEventListener("pointerleave", hide);
    hit.addEventListener("blur", hide);
    hit.addEventListener("focus", function () { show(current); });
    hit.addEventListener("keydown", function (e) {
      if (e.key === "ArrowLeft") { show(current - 1); e.preventDefault(); }
      else if (e.key === "ArrowRight") { show(current + 1); e.preventDefault(); }
      else if (e.key === "Escape") { hide(); }
    });

    // tableau des données (même information, sans survol)
    var tableId = container.id + "-table";
    var details = document.getElementById(tableId);
    if (details) {
      var tbody = details.querySelector("tbody");
      tbody.textContent = "";
      points.slice().reverse().forEach(function (p) {
        var tr = document.createElement("tr");
        var td = document.createElement("td");
        td.textContent = fmtTime(p.t, span > 86400);
        tr.appendChild(td);
        cfg.series.forEach(function (s) {
          var cell = document.createElement("td");
          cell.className = "num";
          cell.textContent = p[s.key] == null ? "—" : fmt(p[s.key]);
          tr.appendChild(cell);
          if (data.aggregated && s.peak !== false) {
            var peak = document.createElement("td");
            peak.className = "num";
            peak.textContent = p[s.key + "_max"] == null ? "—" : fmt(p[s.key + "_max"]);
            tr.appendChild(peak);
          }
        });
        tbody.appendChild(tr);
      });
      details.querySelectorAll("[data-peak-col]").forEach(function (th) { th.hidden = !data.aggregated; });
    }
  }

  /* --- chiffres clés de chaque carte ------------------------------------- */

  function fillSummary() {
    root.querySelectorAll("[data-summary]").forEach(function (node) {
      var parts = node.getAttribute("data-summary").split(":"); // clé:stat:format
      var s = state.data.summary[parts[0]];
      var v = s ? s[parts[1]] : null;
      node.textContent = v == null ? "—" : (FORMATS[parts[2]] || FORMATS.pct)(v);
    });
    var since = root.querySelector("[data-collecting-since]");
    if (since && state.data.collecting_since) {
      since.textContent = "Mesures enregistrées depuis le " + fmtTime(state.data.collecting_since, true) +
        (state.data.aggregated ? " · points regroupés par " + Math.round(state.data.step / 60) + " min (moyenne et pic)" : " · une mesure toutes les 15 s");
    }
  }

  function drawAll() {
    if (!state.data) return;
    root.querySelectorAll("[data-metric-chart]").forEach(draw);
    fillSummary();
  }

  /* --- chargement --------------------------------------------------------- */

  function load() {
    root.classList.add("is-loading");
    fetch("/history/data?range=" + encodeURIComponent(state.range), { headers: { Accept: "application/json" }, cache: "no-store" })
      .then(function (res) {
        if (res.status === 401) { window.location.href = "/login?next=/history"; return null; }
        if (!res.ok) throw new Error(String(res.status));
        return res.json();
      })
      .then(function (data) {
        if (!data) return;
        state.data = data;
        drawAll();
      })
      .catch(function () {
        var err = root.querySelector("[data-history-error]");
        if (err) err.hidden = false;
      })
      .then(function () { root.classList.remove("is-loading"); });
  }

  root.querySelectorAll("[data-range-button]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      state.range = btn.getAttribute("data-range-button");
      root.querySelectorAll("[data-range-button]").forEach(function (b) {
        var on = b === btn;
        b.classList.toggle("is-active", on);
        b.setAttribute("aria-pressed", on ? "true" : "false");
      });
      try { history.replaceState(null, "", "/history?range=" + state.range); } catch (e) { /* sans importance */ }
      load();
    });
  });

  var resizeTimer;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(drawAll, 150);
  });

  // rafraîchissement automatique toutes les minutes (onglet visible)
  setInterval(function () { if (!document.hidden) load(); }, 60000);

  load();
})();
