/* Dashboard: vacancy publication + Google indexing status */
"use strict";

(() => {
  const { api, esc, pill } = App;
  const intervalMs = 6000;
  let timer = null;

  function setKpis(counts) {
    const values = [
      counts.total || 0,
      counts.open || 0,
      counts.sent || 0,
      counts.waiting || 0,
      counts.crawled || 0,
      counts.indexed || 0,
      counts.notIndexed || 0,
      counts.unknown || 0,
    ];
    const host = document.getElementById("job-kpi-grid");
    if (!host) return;

    const labels = [
      ["Total vacancies", "all published /jobs pages"],
      ["Open", "currently active"],
      ["Google accepted", "notification accepted"],
      ["Auto retry / queued", "handled automatically"],
      ["Crawled", "Search Console crawl evidence"],
      ["Indexed", "Search Console index evidence"],
      ["Not indexed", "Search Console says not indexed"],
      ["Unknown", "no authoritative evidence yet"],
    ];

    host.innerHTML = labels.map(([label, foot], i) => `
      <div class="kpi">
        <div class="kpi-top"><span class="kpi-label">${esc(label)}</span></div>
        <div class="kpi-value">${values[i]}</div>
        <div class="kpi-foot">${esc(foot)}</div>
      </div>`
    ).join("");
  }

  function renderRows(items) {
    const host = document.getElementById("real-job-dashboard-rows");
    if (!host) return;

    if (!items.length) {
      host.innerHTML = '<tr><td colspan="10" class="small faint">No published vacancies yet. Use Add URL to upload a CSV/XLSX sheet.</td></tr>';
      return;
    }

    host.innerHTML = items.map((p) => {
      const j = p.job || {};
      const g = p.gsc || {};
      const q = p.queue || {};
      const loc = [j.city, j.region, j.country].filter(Boolean).join(", ") || "—";

      const details = [
        ["Title", j.title],
        ["Company", j.company],
        ["Company URL", j.company_url],
        ["Job details", p.sourceUrl || j.job_details],
        ["Apply URL", j.apply_url],
        ["Description", j.description],
        ["Qualifications", j.qualifications],
        ["Employment type", j.employment_type],
        ["City", j.city],
        ["Region", j.region],
        ["Country", j.country],
        ["Date posted", j.date_posted],
        ["Valid through", j.valid_through],
        ["Public URL", p.publicUrl],
        ["Published", p.publishedAt],
        ["Updated", p.updatedAt],
        ["Vacancy status", p.status],
        ["Google notification", p.notificationStatus],
        ["Queue status", q.status],
        ["Attempts", q.attempts],
        ["Next retry", q.nextAttemptAt],
        ["Queue error", q.error],
        ["Crawl status", g.crawlStatus],
        ["Index status", g.indexStatus],
        ["GSC last checked", g.lastCheckedAt],
        ["GSC next check", g.nextCheckAt],
        ["GSC polling", g.polling ? "AUTO" : "—"],
        ["GSC poll sequence", g.pollSequence],
      ].filter(([, v]) => v !== undefined && v !== null && String(v).trim() !== "")
       .map(([k, v]) => '<div class="small"><span class="faint">' + esc(k) + ':</span> ' + esc(String(v)) + '</div>')
       .join("");

      return `
        <tr>
          <td>
            <a href="${esc(p.path)}" target="_blank" rel="noopener">#${esc(p.number)} · ${esc(j.title || "Untitled")}</a>
            <div class="small faint">${esc(p.status)}</div>
          </td>
          <td>${esc(j.company || "—")}</td>
          <td>${esc(loc)}</td>
          <td>${p.sourceUrl ? '<a href="' + esc(p.sourceUrl) + '" target="_blank" rel="noopener">Source ↗</a>' : "—"}</td>
          <td>${j.apply_url ? '<a href="' + esc(j.apply_url) + '" target="_blank" rel="noopener">Apply ↗</a>' : "—"}</td>
          <td class="small">${esc(p.publishedAt || "—")}</td>
          <td>${pill(p.notificationStatus || "NOT_REQUESTED")}</td>
          <td>${pill(g.crawlStatus || "UNKNOWN")}</td>
          <td>${pill(g.indexStatus || "UNKNOWN")}</td>
          <td class="small">${pill(q.status || "—")}<div class="faint">${q.attempts || 0} attempt(s)</div></td>
        </tr>
        <tr>
          <td colspan="10" style="padding-top:0">
            <details>
              <summary class="small" style="cursor:pointer">View all vacancy data · #${esc(p.number)}</summary>
              <div class="evidence-card mt-8" style="display:grid;gap:5px">
                ${details || '<span class="small faint">No additional vacancy data.</span>'}
              </div>
            </details>
          </td>
        </tr>`;
    }).join("");
  }

  async function refresh() {
    const live = document.getElementById("live-dot");
    try {
      const data = await api("/api/real-jobs/dashboard");
      setKpis(data.counts || {});
      renderRows(data.items || []);
      if (live) live.style.background = "var(--green)";
    } catch (e) {
      if (live) live.style.background = "var(--red)";
    }
  }

  function start() {
    refresh();
    timer = setInterval(refresh, intervalMs);
  }

  window.addEventListener("beforeunload", () => {
    if (timer) clearInterval(timer);
  });

  document.addEventListener("DOMContentLoaded", start);
})();
