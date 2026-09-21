/* monitoring.js — monitoring page: evidence table, probes, observations, GSC inspection */
"use strict";

(() => {
  const { api, esc, pill, timeAgo, fmtDate, toast, openModal, closeModal } = App;
  let items = [];

  function el(id) { return document.getElementById(id); }

  function evidenceBlock(label, ev, extra) {
    if (!ev || Object.keys(ev).length === 0) {
      return `<div class="evidence-card"><span class="ev-source">${label}</span>
        <span class="faint">No evidence recorded yet.</span></div>`;
    }
    const rows = Object.entries(ev)
      .filter(([k]) => k !== "reason")
      .map(([k, v]) => `<div class="meta-list" style="grid-template-columns:150px 1fr"><dt>${esc(k.replace(/_/g, " "))}</dt><dd>${esc(v === null || v === undefined ? "—" : String(v))}</dd></div>`)
      .join("");
    return `<div class="evidence-card"><span class="ev-source">${label}</span>
      ${rows}
      ${ev.reason ? `<div class="faint small mt-8">${esc(ev.reason)}</div>` : ""}
      ${extra || ""}</div>`;
  }

  function render() {
    const tbody = el("mon-tbody");
    const empty = el("mon-empty");
    if (!items.length) {
      tbody.innerHTML = "";
      empty.style.display = "block";
      return;
    }
    empty.style.display = "none";
    tbody.innerHTML = items
      .map((m) => {
        const title = m.title || "—";
        return `<tr data-id="${m.id}">
          <td><div class="cell-main">${esc(title)}</div>
            <div class="cell-sub mono">${esc(m.original_url || "")}</div></td>
          <td>${pill(m.status)}</td>
          <td>${pill(m.discovery_status)}</td>
          <td>${pill(m.crawl_status)}</td>
          <td>${pill(m.index_status)}
            <div class="cell-sub" title="${esc((m.index_evidence || {}).reason || (m.third_party_note || {}).reason || "")}">
              ${esc((m.index_evidence || {}).source || "no source — no authoritative evidence")}
            </div></td>
          <td class="cell-sub">${
            m.last_probe_at
              ? `${timeAgo(m.last_probe_at)} <span class="faint">(HTTP ${esc(m.last_probe_status || "?")})</span>`
              : "never"
          }</td>
          <td class="right">
            <div class="flex" style="justify-content:flex-end;gap:4px">
              <button class="btn btn-sm" data-act="probe" title="Technical Server Probe">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" width="14" height="14"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>
                Probe
              </button>
              <button class="btn btn-sm" data-act="observe" title="Search visibility observation (non-authoritative)">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" width="14" height="14"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
                Observe
              </button>
              <button class="btn btn-sm" data-act="inspect" title="Search Console URL inspection (own pages only)">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" width="14" height="14"><path d="M9 11H3v10h6zM15 3H9v10h6zM21 11h-6v10h6z"/></svg>
                Inspect
              </button>
              <a class="btn btn-sm btn-ghost" href="/pdfs/${m.id}">Detail</a>
            </div>
          </td>
        </tr>`;
      })
      .join("");
    bind();
  }

  function bind() {
    el("mon-tbody").querySelectorAll("tr").forEach((tr) => {
      const m = items.find((x) => String(x.id) === tr.dataset.id);
      if (!m) return;
      tr.querySelectorAll("[data-act]").forEach((btn) =>
        btn.addEventListener("click", () => {
          const act = btn.dataset.act;
          if (act === "probe") {
            toast("Probe queued", "Technical Server Probe scheduled (our server, honest UA — not a search-engine crawl).", "info");
            run(`/api/monitoring/${m.id}/probe`);
          } else if (act === "observe") {
            toast("Observation queued", "Search visibility observation scheduled (non-authoritative).", "info");
            run(`/api/monitoring/${m.id}/observe`);
          } else if (act === "inspect") {
            run(`/api/monitoring/${m.id}/inspect`);
          }
        })
      );
    });
  }

  async function run(path) {
    try {
      const r = await api(path, { method: "POST" });
      toast("Job queued", `Job #${r.job_id} queued.`, "success");
      setTimeout(load, 1500);
    } catch (e) {
      toast("Action failed", e.message, "error");
    }
  }

  function openDetail(m) {
    const ev = m.index_evidence || {};
    const cev = m.crawl_evidence || {};
    const open = openModal(
      `Monitoring — ${m.title || m.id}`,
      `
      <div class="meta-list mb-16">
        <dt>Original URL</dt><dd class="mono">${esc(m.original_url || "—")}</dd>
        <dt>Source domain</dt><dd>${esc(m.source_domain || "—")}</dd>
        <dt>Last probe</dt><dd>${m.last_probe_at ? `${fmtDate(m.last_probe_at)} (HTTP ${esc(m.last_probe_status || "?")})` : "never"}</dd>
      </div>
      <div class="grid-2 mb-8">
        ${evidenceBlock("Index evidence", ev)}
        ${evidenceBlock("Crawl evidence", cev)}
      </div>
      <div class="evidence-card">
        <span class="ev-source">Why index status is what it is</span>
        <span class="small" style="color:var(--muted)">${esc((m.third_party_note || {}).reason || "See evidence above.")}</span>
      </div>`,
      `<button class="btn" type="button" id="m-close">Close</button>`
    );
    open.querySelector("#m-close").addEventListener("click", closeModal);
  }

  async function load() {
    try {
      const data = await api("/api/monitoring");
      items = data.items;
      render();
    } catch (e) {
      toast("Failed to load monitoring", e.message, "error");
    }
  }

  function start() {
    load();
    setInterval(load, 8000);
    el("mon-tbody").addEventListener("dblclick", (e) => {
      const tr = e.target.closest("tr");
      const m = items.find((x) => String(x.id) === tr?.dataset.id);
      if (m) openDetail(m);
    });
  }

  document.addEventListener("DOMContentLoaded", start);
})();
