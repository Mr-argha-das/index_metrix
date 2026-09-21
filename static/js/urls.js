/* urls.js — URLs table page: filters, pagination, actions (retry/delete), submit form */
"use strict";

(() => {
  const { api, esc, pill, fmtDate, timeAgo, fmtBytes, confirmModal, toast } = App;
  let state = { page: 1, per_page: 25, status: "", q: "" };
  let data = null;

  const FILTERS = [
    ["", "All"],
    ["RECEIVED", "Received"],
    ["VALIDATING", "Validating"],
    ["PDF_ANALYZING", "Analyzing"],
    ["PAGE_PUBLISHED", "Published"],
    ["INVALID", "Invalid"],
    ["PDF_INVALID", "Invalid PDF"],
    ["FAILED", "Failed"],
  ];

  function el(id) { return document.getElementById(id); }

  function buildFilters() {
    const host = el("url-filters");
    if (!host) return;
    host.innerHTML = FILTERS.map(
      ([val, label]) => `<button class="tab ${state.status === val ? "active" : ""}" data-status="${val}">${label}</button>`
    ).join("");
    host.querySelectorAll("button").forEach((b) =>
      b.addEventListener("click", () => {
        state.status = b.dataset.status;
        state.page = 1;
        buildFilters();
        load();
      })
    );
  }

  function render() {
    if (!data) return;
    const tbody = el("urls-tbody");
    const empty = el("urls-empty");
    if (!data.items.length) {
      tbody.innerHTML = "";
      empty.style.display = "block";
    } else {
      empty.style.display = "none";
      tbody.innerHTML = data.items
        .map((p) => {
          const title = p.title || "Untitled resource";
          const states = p.states || {};
          return `<tr>
            <td>
              <div class="cell-main">${esc(title)}</div>
              <div class="cell-sub mono">${esc(p.normalized_url || "")}</div>
            </td>
            <td class="cell-sub">${esc(p.source_domain || "—")}</td>
            <td>${p.html_metadata ? pill("HTML") : p.classification ? pill(p.classification) : '<span class="faint">—</span>'}</td>
            <td>${esc(p.http_status ?? "—")}</td>
            <td>${fmtBytes(p.content_length)}</td>
            <td class="cell-sub">${p.page_count ? p.page_count + " pages" : "—"}</td>
            <td>
              ${
                p.page
                  ? `<a href="${p.page.page_kind === "demo-job" ? "/jobs/" + esc(p.page.job_number) : "/pdf/" + esc(p.page.slug)}" target="_blank" rel="noopener" class="btn btn-sm btn-ghost">Open page</a>`
                  : '<span class="faint">—</span>'
              }
            </td>
            <td>${pill(p.status)}${p.error ? `<div class="small mt-8" style="color:var(--red);max-width:300px;overflow-wrap:anywhere"><strong>Failure reason:</strong> ${esc(p.error)}</div><a class="small" href="/pdfs/${p.id}">View diagnostics</a>` : ""}</td>
            <td title="Direct request-indexing is unsupported; normal discovery remains active.">${pill(states.referenceSubmissionStatus)}</td>
            <td>${pill(states.referenceCrawlStatus)}</td>
            <td>${pill(states.referenceIndexStatus)}</td>
            <td>${pill(states.externalDiscoveryStatus)}</td>
            <td>${pill(states.externalCrawlStatus)}</td>
            <td>${pill(states.externalIndexStatus)}</td>
            <td class="cell-sub">${timeAgo(p.last_checked_at || p.last_probe_at)}</td>
            <td class="right">
              <div class="flex" style="justify-content:flex-end;gap:4px">
                <a class="btn btn-sm btn-ghost" href="/pdfs/${p.id}" title="Open detail">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></svg>
                </a>
                <button class="btn btn-sm btn-ghost" data-action="retry" data-id="${p.id}" title="Retry processing">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" width="14" height="14"><path d="M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6"/></svg>
                </button>
                <button class="btn btn-sm btn-ghost" data-action="delete" data-id="${p.id}" title="Delete" style="color:var(--red)">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" width="14" height="14"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg>
                </button>
              </div>
            </td>
          </tr>`;
        })
        .join("");
    }
    // pagination
    const info = el("pagination-info");
    if (info) info.textContent = `${data.total} URL${data.total === 1 ? "" : "s"} · page ${data.page}/${Math.max(1, Math.ceil(data.total / data.per_page))}`;
    const prev = el("page-prev"), next = el("page-next");
    if (prev) prev.disabled = state.page <= 1;
    if (next) next.disabled = state.page * data.per_page >= data.total;
  }

  async function load() {
    const qs = new URLSearchParams();
    if (state.status) qs.set("status", state.status);
    if (state.q) qs.set("q", state.q);
    qs.set("page", state.page);
    qs.set("per_page", state.per_page);
    try {
      data = await api(`/api/pdfs?${qs.toString()}`);
      render();
    } catch (e) {
      toast("Error", e.message, "error");
    }
  }

  function bindActions() {
    const tbody = el("urls-tbody");
    if (!tbody) return;
    tbody.querySelectorAll("[data-action]").forEach((btn) =>
      btn.addEventListener("click", async () => {
        const id = btn.dataset.id;
        if (btn.dataset.action === "retry") {
          try {
            await api(`/api/pdfs/${id}/retry`, { method: "POST" });
            toast("Queued", "Processing retry started.", "success");
            setTimeout(load, 800);
          } catch (e) {
            toast("Retry failed", e.message, "error");
          }
        } else if (btn.dataset.action === "delete") {
          const ok = await confirmModal("Delete this resource?", "This removes the resource record and its dedicated page (and therefore from sitemap/RSS). This cannot be undone.", { confirmLabel: "Delete", danger: true });
          if (!ok) return;
          try {
            await api(`/api/pdfs/${id}`, { method: "DELETE" });
            toast("Deleted", "Resource record removed.", "success");
            load();
          } catch (e) {
            toast("Delete failed", e.message, "error");
          }
        }
      })
    );
  }

  function bindControls() {
    const q = el("url-search");
    if (q) {
      let t = null;
      q.addEventListener("input", () => {
        clearTimeout(t);
        t = setTimeout(() => {
          state.q = q.value.trim();
          state.page = 1;
          load();
        }, 350);
      });
    }
    const prev = el("page-prev"), next = el("page-next");
    if (prev) prev.addEventListener("click", () => (state.page = Math.max(1, state.page - 1), load()));
    if (next) next.addEventListener("click", () => (state.page++, load()));

    // submit form (textarea + optional file)
    const form = el("quick-submit");
    if (form) {
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const text = el("quick-submit-text").value;
        const file = el("quick-submit-file")?.files?.[0];
        const btn = el("quick-submit-btn");
        btn.disabled = true;
        try {
          let result;
          if (file) {
            const fd = new FormData();
            fd.append("file", file);
            result = await api("/api/pdfs/file", { method: "POST", body: fd });
          } else if (text.trim()) {
            const urls = text.split(/\r?\n|,/).map((s) => s.trim()).filter(Boolean);
            result = await api("/api/pdfs/bulk", { method: "POST", body: { urls } });
          } else {
            toast("Nothing to submit", "Paste URLs or choose a file first.", "warn");
            btn.disabled = false;
            return;
          }
          const n = result.accepted.length;
          const d = result.duplicates.length;
          const inv = result.invalid.length;
          let msg = `${n} URL${n === 1 ? "" : "s"} accepted`;
          if (d) msg += `, ${d} duplicate${d === 1 ? "" : "s"} skipped`;
          if (inv) msg += `, ${inv} invalid`;
          toast("Submission complete", msg, inv ? "warn" : "success");
          if (n) {
            el("quick-submit-text").value = "";
            if (el("quick-submit-file")) el("quick-submit-file").value = "";
            load();
          }
        } catch (e2) {
          toast("Submission failed", e2.message, "error");
        } finally {
          btn.disabled = false;
        }
      });
    }
  }

  function start() {
    buildFilters();
    bindControls();
    load();
    setInterval(load, 8000);
    // re-bind actions after each load
    const obs = new MutationObserver(bindActions);
    const tbody = el("urls-tbody");
    if (tbody) obs.observe(tbody, { childList: true, subtree: true });
  }

  document.addEventListener("DOMContentLoaded", start);
})();
