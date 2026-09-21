/* dashboard.js — real-time dashboard (polling every N ms, no websockets) */
"use strict";

(() => {
  const { api, esc, pill, fmtBytes, shortUrl, timeAgo } = App;
  let intervalMs = 6000;
  let timer = null;
  let lastData = null;

  const KPI_DEFS = [
    ["total", "Total URLs", "k-total", "grid"],
    ["valid", "Valid PDFs", "k-valid", "file"],
    ["invalid", "Invalid URLs", "k-invalid", "x"],
    ["processing", "Processing", "k-processing", "spin"],
    ["published", "Published Pages", "k-published", "book"],
    ["discovery_pending", "Discovery Pending", "k-discovery", "search"],
    ["crawl_checked", "Crawl Checked", "k-crawl", "eye"],
    ["indexed", "Indexed", "k-indexed", "check"],
    ["unknown", "Unknown", "k-unknown", "question"],
  ];

  const KPI_ICONS = {
    grid: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>',
    file: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>',
    x: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="m9 9 6 6M15 9l-6 6"/></svg>',
    spin: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M21 12a9 9 0 1 1-6.2-8.56"/></svg>',
    book: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>',
    search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>',
    eye: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></svg>',
    check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="m8.5 12.5 2.5 2.5 5-6"/></svg>',
    question: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 1 1 3.4 2.3c-.8.34-1.4 1-1.4 1.9v.3M12 17h.01"/></svg>',
  };

  const STATUS_COLORS = {
    PAGE_PUBLISHED: "#34d399",
    DISCOVERY_PENDING: "#22d3ee",
    PDF_VALID: "#60a5fa",
    VALID: "#60a5fa",
    VALIDATING: "#fbbf24",
    PDF_ANALYZING: "#fbbf24",
    PAGE_GENERATING: "#fbbf24",
    RECEIVED: "#93c5fd",
    INVALID: "#f87171",
    PDF_INVALID: "#f87171",
    PDF_ANALYSIS_FAILED: "#fb923c",
    FAILED: "#f87171",
  };

  function renderKpis(stats) {
    const host = document.getElementById("kpi-grid");
    if (!host) return;
    host.innerHTML = KPI_DEFS.map(([key, label, cls, icon]) => `
      <div class="kpi ${cls}">
        <div class="kpi-top">
          <span class="kpi-label">${esc(label)}</span>
          <span class="kpi-icon">${KPI_ICONS[icon]}</span>
        </div>
        <div class="kpi-value" id="kpi-${key}">${stats[key] ?? 0}</div>
        <div class="kpi-foot">${footFor(key)}</div>
      </div>`).join("");
  }

  function footFor(key) {
    switch (key) {
      case "total": return "all submitted URLs";
      case "valid": return "passed PDF validation";
      case "invalid": return "rejected or unresolvable";
      case "processing": return "in the queue right now";
      case "published": return "dedicated pages live";
      case "discovery_pending": return "awaiting search engines";
      case "crawl_checked": return "evidence-based";
      case "indexed": return "with authoritative evidence";
      case "unknown": return "no evidence yet";
      default: return "";
    }
  }

  function renderDonut(byStatus) {
    const el = document.getElementById("status-donut");
    const legend = document.getElementById("status-legend");
    if (!el || !legend) return;
    const entries = Object.entries(byStatus || {}).sort((a, b) => b[1] - a[1]);
    const total = entries.reduce((s, [, v]) => s + v, 0);
    if (!total) {
      el.innerHTML = "";
      legend.innerHTML = '<div class="muted small">No data yet.</div>';
      return;
    }
    const size = 150, stroke = 18, r = (size - stroke) / 2, c = 2 * Math.PI * r;
    let offset = 0;
    const segs = entries
      .map(([status, count]) => {
        const frac = count / total;
        const color = STATUS_COLORS[status] || "#64748b";
        const dash = frac * c;
        const seg = `<circle r="${r}" cx="${size / 2}" cy="${size / 2}" fill="none"
          stroke="${color}" stroke-width="${stroke}"
          stroke-dasharray="${Math.max(dash - 1.5, 0.001)} ${c - dash + 1.5}"
          stroke-dashoffset="${-offset}" transform="rotate(-90 ${size / 2} ${size / 2})"
          style="transition: stroke-dasharray .5s ease"><title>${esc(status)}: ${count}</title></circle>`;
        offset += dash;
        return seg;
      })
      .join("");
    el.setAttribute("viewBox", `0 0 ${size} ${size}`);
    el.innerHTML = segs;
    document.querySelector(".donut-center .num").textContent = total;
    legend.innerHTML = entries
      .map(
        ([status, count]) => `
        <div class="li"><span class="sw" style="background:${STATUS_COLORS[status] || "#64748b"}"></span>
        ${esc(status.replace(/_/g, " ").toLowerCase())}<span class="val">${count}</span></div>`
      )
      .join("");
  }

  function renderBars(perDay) {
    const host = document.getElementById("submissions-bars");
    if (!host || !perDay) return;
    const entries = Object.entries(perDay);
    const max = Math.max(1, ...entries.map(([, v]) => v));
    host.innerHTML = entries
      .map(([day, v]) => {
        const h = Math.round((v / max) * 96);
        return `<div class="bar-col">
          <div class="bar" style="height:${Math.max(h, 3)}px"><span class="bar-tip">${v}</span></div>
          <span class="bar-lbl">${day.slice(5)}</span>
        </div>`;
      })
      .join("");
  }

  function renderTable(pdfs) {
    const tbody = document.getElementById("recent-tbody");
    const empty = document.getElementById("recent-empty");
    if (!tbody) return;
    if (!pdfs || !pdfs.length) {
      tbody.innerHTML = "";
      if (empty) empty.style.display = "block";
      return;
    }
    if (empty) empty.style.display = "none";
    tbody.innerHTML = pdfs
      .map((p) => {
        const title = p.title || "Untitled PDF";
        return `<tr>
          <td><div class="cell-main">${esc(title)}</div>
            <div class="cell-sub">${esc(shortUrl(p.normalized_url))}</div></td>
          <td class="cell-sub">${esc(p.source_domain || "—")}</td>
          <td>${pill(p.status)}</td>
          <td>${pill(p.discovery_status)}</td>
          <td>${pill(p.crawl_status)}</td>
          <td>${pill(p.index_status)}</td>
          <td class="cell-sub">${timeAgo(p.updated_at)}</td>
          <td class="right">
            <a class="btn btn-sm btn-ghost" href="/pdfs/${p.id}">Details</a>
          </td>
        </tr>`;
      })
      .join("");
  }

  function renderEvents(events) {
    const host = document.getElementById("event-feed");
    if (!host) return;
    if (!events || !events.length) {
      host.innerHTML = '<div class="muted small" style="padding:14px">No events yet — submit a PDF URL to get started.</div>';
      return;
    }
    host.innerHTML = events
      .map((e) => {
        const color =
          e.status === "ERROR" ? "var(--red)" :
          e.status === "SUCCESS" ? "var(--green)" :
          e.status === "WARN" ? "var(--amber)" : "var(--blue)";
        const soft =
          e.status === "ERROR" ? "var(--red-soft)" :
          e.status === "SUCCESS" ? "var(--green-soft)" :
          e.status === "WARN" ? "var(--amber-soft)" : "var(--blue-soft)";
        const name = e.pdf_id ? `PDF #${e.pdf_id}` : e.user_id ? `User #${e.user_id}` : "system";
        return `<div class="event-row">
          <span class="e-icon" style="background:${soft};color:${color}">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="12" cy="12" r="4"/></svg>
          </span>
          <span class="e-body">
            <span class="e-type" style="color:${color}">${esc(e.event_type.replace(/_/g, " ").toLowerCase())}</span>
            <span class="e-msg">${esc(e.message || "")}</span>
          </span>
          <span class="e-time" title="${esc(name)}">${timeAgo(e.created_at)}</span>
        </div>`;
      })
      .join("");
  }

  function renderQueue(q) {
    const host = document.getElementById("queue-mini");
    if (!host || !q) return;
    host.innerHTML = `
      <div class="qm"><div class="qm-num">${q.pending || 0}</div><div class="qm-lbl">Pending</div></div>
      <div class="qm"><div class="qm-num">${q.running || 0}</div><div class="qm-lbl">Running</div></div>
      <div class="qm"><div class="qm-num">${q.completed || 0}</div><div class="qm-lbl">Completed</div></div>
      <div class="qm"><div class="qm-num" style="color:${q.failed ? "var(--red)" : "var(--text)"}">${q.failed || 0}</div><div class="qm-lbl">Failed</div></div>`;
    const prog = document.getElementById("queue-progress");
    if (prog && (q.pending + q.running + q.completed)) {
      const total = q.pending + q.running + q.completed;
      prog.style.width = `${Math.round(((q.completed || 0) / total) * 100)}%`;
    }
  }

  async function refresh() {
    try {
      const data = await api("/api/dashboard/stats");
      lastData = data;
      const first = !document.getElementById("kpi-total");
      if (first) {
        renderKpis(data.stats);
        renderDonut(data.by_status);
        renderBars(data.per_day);
      } else {
        for (const [key] of KPI_DEFS) {
          const el = document.getElementById(`kpi-${key}`);
          if (el && el.textContent !== String(data.stats[key] ?? 0)) {
            el.textContent = data.stats[key] ?? 0;
            el.style.transition = "color .3s";
          }
        }
        renderDonut(data.by_status);
        renderBars(data.per_day);
      }
      renderTable(data.recent_pdfs);
      renderEvents(data.recent_events);
      renderQueue(data.queue);
      intervalMs = data.polling_interval_ms || intervalMs;
      const live = document.getElementById("live-dot");
      if (live) live.style.background = "var(--green)";
    } catch (e) {
      const live = document.getElementById("live-dot");
      if (live) live.style.background = "var(--red)";
    }
  }

  function start() {
    refresh();
    timer = setInterval(refresh, intervalMs);
  }

  document.addEventListener("DOMContentLoaded", start);
})();
