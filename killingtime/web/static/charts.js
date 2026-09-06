/* Chart helpers built on Chart.js 4. Colours come from CSS custom properties so light/dark stay in sync. */
(function () {
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const palette = () => [1, 2, 3, 4, 5, 6, 7, 8].map((i) => css(`--s${i}`));

  function baseOptions(opts) {
    const grid = css("--grid"), muted = css("--muted"), ink = css("--ink-2");
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: opts.mode || "index", intersect: false },
      plugins: {
        legend: {
          display: opts.legend !== false && (opts.seriesCount || 0) >= 2,
          labels: { color: ink, boxWidth: 10, boxHeight: 10, usePointStyle: true, pointStyle: "circle" },
        },
        tooltip: {
          backgroundColor: css("--surface"), titleColor: css("--ink"), bodyColor: ink, borderColor: css("--border"), borderWidth: 1,
          callbacks: opts.tooltip || {},
        },
        title: { display: false },
      },
      scales: {
        x: {
          title: { display: !!opts.xTitle, text: opts.xTitle, color: muted },
          grid: { color: grid, drawTicks: false }, border: { color: css("--grid") },
          ticks: { color: muted, maxRotation: 0, autoSkip: true, font: { size: 11 } },
          stacked: !!opts.stacked,
        },
        y: {
          title: { display: !!opts.yTitle, text: opts.yTitle, color: muted },
          grid: { color: grid, drawTicks: false }, border: { display: false },
          ticks: { color: muted, precision: 0, font: { size: 11 } },
          beginAtZero: true, stacked: !!opts.stacked, max: opts.yMax,
        },
      },
    };
  }

  function lineChart(canvas, labels, series, opts = {}) {
    const colors = palette();
    const datasets = series.map((s, i) => ({
      label: s.name,
      data: s.data,
      borderColor: s.color || colors[i % colors.length],
      backgroundColor: s.color || colors[i % colors.length],
      borderWidth: s.emphasis ? 3 : 2,
      pointRadius: labels.length > 60 ? 0 : 3,
      pointHoverRadius: 5,
      pointBorderColor: css("--surface"),
      pointBorderWidth: 1,
      stepped: opts.stepped ? "before" : false,
      tension: opts.stepped ? 0 : 0.15,
      spanGaps: true,
      fill: false,
    }));
    return new Chart(canvas, { type: "line", data: { labels, datasets }, options: baseOptions({ ...opts, seriesCount: series.length }) });
  }

  function barChart(canvas, labels, series, opts = {}) {
    const colors = palette();
    const datasets = series.map((s, i) => ({
      label: s.name,
      data: s.data,
      backgroundColor: s.color || colors[i % colors.length],
      borderColor: css("--surface"),
      borderWidth: opts.stacked ? 1 : 0,
      borderRadius: opts.stacked ? 0 : 4,
      borderSkipped: opts.horizontal ? "left" : "bottom",
      maxBarThickness: 28,
      categoryPercentage: 0.7,
      barPercentage: 0.9,
    }));
    const options = baseOptions({ ...opts, seriesCount: series.length });
    if (opts.horizontal) {
      options.indexAxis = "y";
      const x = options.scales.x, y = options.scales.y;
      options.scales.x = { ...y, title: { display: !!opts.yTitle, text: opts.yTitle, color: css("--muted") } };
      options.scales.y = { ...x, beginAtZero: false, ticks: { color: css("--muted"), autoSkip: false, font: { size: 11 } }, grid: { display: false } };
    }
    return new Chart(canvas, { type: "bar", data: { labels, datasets }, options });
  }

  function tableHTML(labels, series, corner = "") {
    const esc = (v) => String(v ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    const fmt = (v) => (v === null || v === undefined ? "–" : typeof v === "number" ? (Number.isInteger(v) ? v : v.toFixed(1)) : v);
    let h = `<table><thead><tr><th>${esc(corner)}</th>${series.map((s) => `<th class="num">${esc(s.name)}</th>`).join("")}</tr></thead><tbody>`;
    labels.forEach((l, i) => {
      h += `<tr><td>${esc(l)}</td>${series.map((s) => `<td class="num">${esc(fmt(s.data[i]))}</td>`).join("")}</tr>`;
    });
    return h + "</tbody></table>";
  }

  /* Card with a chart, a "Table" toggle (the accessible twin), and an optional note. */
  function mount(container, { title, labels, series, type = "bar", horizontal = false, stacked = false, stepped = false, xTitle, yTitle, note, height }) {
    const card = document.createElement("div");
    card.className = "card";
    const id = "c" + Math.random().toString(36).slice(2, 9);
    const h = height || Math.max(260, horizontal ? 28 * labels.length + 60 : 280);
    card.innerHTML = `<h2><span>${title ? title.replace(/</g, "&lt;") : ""}</span><span class="tools"><button type="button" data-toggle="${id}">Table</button></span></h2>
      <div class="chart-wrap" style="height:${h}px"><canvas id="${id}"></canvas></div>
      <div class="table-view" id="${id}-table">${tableHTML(labels, series)}</div>
      ${note ? `<div class="chart-note">${note.replace(/</g, "&lt;")}</div>` : ""}`;
    container.appendChild(card);
    const canvas = card.querySelector("canvas");
    const opts = { xTitle, yTitle, stacked, stepped, horizontal };
    if (type === "line") lineChart(canvas, labels, series, opts);
    else barChart(canvas, labels, series, { ...opts, horizontal: horizontal || type === "hbar", stacked: stacked || type === "stacked" });
    card.querySelector("[data-toggle]").addEventListener("click", (e) => {
      const tv = card.querySelector(".table-view"), cw = card.querySelector(".chart-wrap");
      const show = !tv.classList.contains("show");
      tv.classList.toggle("show", show);
      cw.classList.toggle("hidden", show);
      e.target.textContent = show ? "Chart" : "Table";
    });
    return card;
  }

  /* Render a chart spec produced by the Ask tool. */
  function renderSpec(container, spec) {
    return mount(container, {
      title: spec.title,
      labels: spec.categories,
      series: spec.series,
      type: spec.type,
      xTitle: spec.x_label,
      yTitle: spec.y_label,
      note: spec.note,
    });
  }

  /* Theme toggle: light / dark / system stored in localStorage. */
  function initTheme() {
    let t = null;
    try { t = localStorage.getItem("kt-theme"); } catch (e) { /* ignore */ }
    if (t) document.documentElement.setAttribute("data-theme", t);
    const btn = document.getElementById("theme-btn");
    if (!btn) return;
    const label = () => (btn.textContent = document.documentElement.getAttribute("data-theme") === "dark" ? "☾ Dark" : document.documentElement.getAttribute("data-theme") === "light" ? "☀ Light" : "◐ Auto");
    label();
    btn.addEventListener("click", () => {
      const cur = document.documentElement.getAttribute("data-theme");
      const next = cur === "dark" ? "light" : cur === "light" ? null : "dark";
      if (next) document.documentElement.setAttribute("data-theme", next); else document.documentElement.removeAttribute("data-theme");
      try { next ? localStorage.setItem("kt-theme", next) : localStorage.removeItem("kt-theme"); } catch (e) { /* ignore */ }
      label();
      location.reload();
    });
  }

  window.KT = { lineChart, barChart, mount, renderSpec, tableHTML, palette, css, initTheme };
  document.addEventListener("DOMContentLoaded", initTheme);
})();
